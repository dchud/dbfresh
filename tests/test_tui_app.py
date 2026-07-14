import asyncio
import re
import threading

from textual.widgets import DataTable

from dbfresh import runner
from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.checks import Check, check_id
from dbfresh.engine import Result, Status
from dbfresh.store import Store
from dbfresh.tui.app import DbfreshApp
from dbfresh.tui.screens import HistoryScreen, ObjectDetailScreen, ReportScreen

_OBJECT_ROW_KEY = "s\x1ft"  # source "s", object "t" -- matches GridRow.key's shape


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


def _overall_glyph(table, row_key):
    return table.get_cell(row_key, "overall").plain


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
            table = app.query_one("#dashboard-grid", DataTable)
            # One row for s.t (object-scope), rolled up to the worst of its
            # two checks.
            assert table.row_count == 1
            assert _overall_glyph(table, _OBJECT_ROW_KEY) == "✗"  # FAIL

            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ObjectDetailScreen)
            detail_table = app.screen.query_one(DataTable)
            assert detail_table.row_count == 2
            assert _overall_glyph(detail_table, check_id(_row_count_check())) == "✓"
            assert _overall_glyph(detail_table, check_id(_null_rate_check())) == "✗"

    asyncio.run(scenario())


def test_on_mount_reuses_a_preloaded_config_without_reparsing(tmp_path, monkeypatch):
    async def scenario():
        from dbfresh.config import load_config as real_load_config

        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        preloaded = real_load_config(cfg)

        def fail_if_called(path):
            raise AssertionError("load_config must not run again at mount time")

        monkeypatch.setattr("dbfresh.tui.app.load_config", fail_if_called)

        app = DbfreshApp(
            config_path=cfg, store_path=str(store_path), initial_config=preloaded
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.config is preloaded

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
            table = app.query_one("#dashboard-grid", DataTable)

            # Nothing observed yet: the object row's overall is unknown.
            assert _overall_glyph(table, _OBJECT_ROW_KEY) == "·"

            await pilot.press("r")
            # The Run action starts the check run on a background worker;
            # wait for it to finish before asserting on its results.
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            table = app.query_one("#dashboard-grid", DataTable)
            assert _overall_glyph(table, _OBJECT_ROW_KEY) == "✗"  # null_rate fails

            await pilot.press("enter")
            await pilot.pause()
            detail_table = app.screen.query_one(DataTable)
            assert _overall_glyph(detail_table, check_id(_row_count_check())) == "✓"
            assert _overall_glyph(detail_table, check_id(_null_rate_check())) == "✗"

            assert app.last_run is not None
            assert app.last_run.status == Status.FAIL

    asyncio.run(scenario())


def test_run_action_stays_responsive_and_refreshes_when_the_worker_completes(
    tmp_path,
    monkeypatch,
):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        started = threading.Event()
        release = threading.Event()
        real_run_and_persist = runner.run_and_persist

        def blocking_run_and_persist(config, store, now=None):
            started.set()
            assert release.wait(timeout=2), "test never released the run"
            return real_run_and_persist(config, store, now=now)

        monkeypatch.setattr(runner, "run_and_persist", blocking_run_and_persist)

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()

            await pilot.press("r")
            await pilot.pause()

            # The worker thread is blocked inside the run, but the event
            # loop kept servicing messages meanwhile: the run has started,
            # the dashboard has not been refreshed yet, and the app is
            # still responsive to further queries.
            assert started.wait(timeout=2)
            assert app.last_run is None
            table = app.query_one("#dashboard-grid", DataTable)
            assert _overall_glyph(table, _OBJECT_ROW_KEY) == "·"

            release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert app.last_run is not None
            assert app.last_run.status == Status.FAIL
            table = app.query_one("#dashboard-grid", DataTable)
            assert _overall_glyph(table, _OBJECT_ROW_KEY) == "✗"

    asyncio.run(scenario())


def test_run_action_error_notifies_and_leaves_app_alive(tmp_path, monkeypatch):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        def raising_run_and_persist(config, store, now=None):
            raise RuntimeError("store locked")

        monkeypatch.setattr(runner, "run_and_persist", raising_run_and_persist)

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()

            await pilot.press("r")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            # The app survived the worker error rather than being torn
            # down, and the dashboard/last_run are untouched.
            table = app.query_one("#dashboard-grid", DataTable)
            assert _overall_glyph(table, _OBJECT_ROW_KEY) == "·"
            assert app.last_run is None

            messages = [n.message for n in app._notifications]
            assert any("store locked" in m for m in messages)

    asyncio.run(scenario())


def test_selecting_a_check_row_opens_history_with_its_observations(tmp_path):
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
            # Home grid: one row (s.t) -- enter drills into its checks.
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ObjectDetailScreen)

            # ObjectDetailScreen: row_count is first (config order) --
            # enter on the default cursor position opens its History.
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
            await pilot.press("enter")  # Home -> ObjectDetailScreen
            await pilot.pause()
            await pilot.press("enter")  # ObjectDetailScreen -> HistoryScreen
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
            # The Run action starts the check run on a background worker;
            # wait for it to finish rather than a single pause, so the
            # report below always reflects a completed run instead of
            # racing it.
            await pilot.app.workers.wait_for_complete()
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
            # The Run action starts the check run on a background worker;
            # wait for it to finish rather than a single pause, so the
            # report below always reflects a completed run instead of
            # racing it.
            await pilot.app.workers.wait_for_complete()
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
