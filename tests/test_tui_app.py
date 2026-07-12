import asyncio
import re

from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.checks import Check, check_id
from dbfresh.engine import Result, Status
from dbfresh.store import Store
from dbfresh.tui.app import DbfreshApp
from dbfresh.tui.screens import HistoryScreen, ReportScreen


def _seed_db(path):
    adapter = SqliteAdapter(str(path))
    adapter.rows("CREATE TABLE t (id INTEGER, email TEXT)")
    adapter.rows(
        "INSERT INTO t (id, email) VALUES (1, 'a@x.com'), (2, NULL), (3, NULL)"
    )
    adapter.close()


def _config(path, db):
    path.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
        "checks:\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: row_count\n"
        "    expect: { between: [1, 10] }\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: null_rate\n"
        "    column: email\n"
        "    expect: { max: 0.1 }\n"
    )
    return path


def _row_count_check():
    return Check(source="s", object="t", metric="row_count")


def _null_rate_check():
    return Check(source="s", object="t", metric="null_rate", column="email")


def _find_child(node, name):
    for child in node.children:
        if str(child.label).split(" ")[0] == name:
            return child
    raise AssertionError(f"no child named {name!r} among {list(node.children)}")


def _find_leaf(tree, path):
    node = tree.root
    for name in path:
        node = _find_child(node, name)
    return node


def test_dashboard_reflects_seeded_store_statuses_on_mount(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"
        store = Store(store_path)
        run_id = store.start_run()
        store.record_observation(
            run_id,
            Result(
                object="t",
                metric="row_count",
                status=Status.OK,
                source="s",
                value=3,
                check_id=check_id(_row_count_check()),
            ),
        )
        store.record_observation(
            run_id,
            Result(
                object="t",
                metric="null_rate",
                status=Status.FAIL,
                source="s",
                value=0.9,
                check_id=check_id(_null_rate_check()),
            ),
        )
        store.finish_run(run_id, Status.FAIL)
        store.close()

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#dashboard-tree")

            row_count_leaf = _find_leaf(tree, ["s", "t", "row_count"])
            assert "OK" in str(row_count_leaf.label)

            email_node = _find_leaf(tree, ["s", "t", "email"])
            assert "FAIL" in str(email_node.label)
            null_rate_leaf = _find_child(email_node, "null_rate")
            assert "FAIL" in str(null_rate_leaf.label)

            # object and source nodes roll up to the worst child status.
            t_node = _find_leaf(tree, ["s", "t"])
            s_node = _find_leaf(tree, ["s"])
            assert "FAIL" in str(t_node.label)
            assert "FAIL" in str(s_node.label)

    asyncio.run(scenario())


def test_run_action_updates_dashboard_from_new_observations(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#dashboard-tree")

            # Nothing observed yet: both checks render as unknown.
            row_count_leaf = _find_leaf(tree, ["s", "t", "row_count"])
            assert "unknown" in str(row_count_leaf.label)

            await pilot.press("r")
            await pilot.pause()

            tree = app.query_one("#dashboard-tree")
            row_count_leaf = _find_leaf(tree, ["s", "t", "row_count"])
            assert "OK" in str(row_count_leaf.label)  # 3 rows, between 1 and 10

            email_node = _find_leaf(tree, ["s", "t", "email"])
            assert "FAIL" in str(email_node.label)  # 2/3 null > max 0.1

            assert app.last_run is not None
            assert app.last_run.status == Status.FAIL

    asyncio.run(scenario())


def test_selecting_a_check_node_opens_history_with_its_observations(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"
        store = Store(store_path)
        cid = check_id(_row_count_check())
        for value in (3, 5):
            run_id = store.start_run()
            store.record_observation(
                run_id,
                Result(
                    object="t",
                    metric="row_count",
                    status=Status.OK,
                    source="s",
                    value=value,
                    check_id=cid,
                ),
            )
            store.finish_run(run_id, Status.OK)
        store.close()

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            # dbfresh (root) -> s -> t -> row_count: three downs lands on it.
            await pilot.press("down", "down", "down")
            tree = app.query_one("#dashboard-tree")
            assert tree.cursor_node is not None
            assert str(tree.cursor_node.label).startswith("row_count")

            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, HistoryScreen)
            text = app.screen.query_one("#history-text").content
            assert "3.0" in str(text)
            assert "5.0" in str(text)
            assert cid in str(text)

    asyncio.run(scenario())


def _calendar_config(path, db):
    path.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
        "calendar:\n"
        "  timezone: America/New_York\n"
        "checks:\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: row_count\n"
        "    expect: { between: [1, 10] }\n"
    )
    return path


_OFFSET_TIMESTAMP = re.compile(r"T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}")


def test_history_screen_uses_calendar_timezone(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _calendar_config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"
        store = Store(store_path)
        cid = check_id(_row_count_check())
        run_id = store.start_run()
        store.record_observation(
            run_id,
            Result(
                object="t",
                metric="row_count",
                status=Status.OK,
                source="s",
                value=3,
                check_id=cid,
            ),
        )
        store.finish_run(run_id, Status.OK)
        store.close()

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("down", "down", "down")
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, HistoryScreen)
            text = str(app.screen.query_one("#history-text").content)
            assert _OFFSET_TIMESTAMP.search(text)

    asyncio.run(scenario())


def test_report_screen_uses_calendar_timezone(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _calendar_config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("r")
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()

            assert isinstance(app.screen, ReportScreen)
            text = str(app.screen.query_one("#report-text").content)
            header = text.splitlines()[0]
            assert _OFFSET_TIMESTAMP.search(header)

    asyncio.run(scenario())


def test_report_action_shows_last_in_session_run_digest(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("r")
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()

            assert isinstance(app.screen, ReportScreen)
            text = str(app.screen.query_one("#report-text").content)
            assert "DATA CHECK REPORT" in text
            assert "null_rate" in text  # the one failing check is listed

    asyncio.run(scenario())


def test_report_action_before_any_run_shows_placeholder(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()

            assert isinstance(app.screen, ReportScreen)
            text = str(app.screen.query_one("#report-text").content)
            assert "no run recorded" in text

    asyncio.run(scenario())
