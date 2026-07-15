"""Pilot tests for editing and deleting checks from ObjectDetailScreen --
the connection-free counterpart to ConfigureScreen's own in-Propose
existing-check editing, reached straight from the Home grid's drill-in."""

import asyncio

import yaml
from textual.widgets import Button, DataTable

from dbfresh.checks import Check, check_id
from dbfresh.config import load_config
from dbfresh.tui.app import DbfreshApp
from dbfresh.tui.screens import ObjectDetailScreen

_OBJECT_ROW_KEY = "s\x1ft"


def _config(path, db):
    path.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
        "checks:\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: row_count\n"
        "    expect:\n"
        "      between: [1, 1000]\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: null_rate\n"
        "    column: email\n"
        "    expect:\n"
        "      max: 0.05\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: schema\n"
        "    expect:\n"
        "      unchanged: true\n"
    )
    return path


def _seed_db(path):
    from dbfresh.adapters.sqlite import SqliteAdapter

    adapter = SqliteAdapter(str(path))
    adapter.rows("CREATE TABLE t (id INTEGER, email TEXT)")
    adapter.close()


def _row_count_check():
    return Check(source="s", object="t", metric="row_count")


def _null_rate_check():
    return Check(source="s", object="t", metric="null_rate", column="email")


def _schema_check():
    return Check(source="s", object="t", metric="schema")


async def _open_object_detail(pilot):
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()
    assert isinstance(pilot.app.screen, ObjectDetailScreen)


def test_object_detail_shows_save_and_delete_affordances(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)

            # row_count/between: two Inputs + Save + Delete.
            assert app.screen.query_one("#detail-lo-0").value == "1"
            assert app.screen.query_one("#detail-hi-0").value == "1000"
            app.screen.query_one("#detail-save-0", Button)
            app.screen.query_one("#detail-delete-0", Button)

            # null_rate/max: one Input + Save + Delete.
            assert app.screen.query_one("#detail-value-1").value == "0.05"
            app.screen.query_one("#detail-save-1", Button)
            app.screen.query_one("#detail-delete-1", Button)

            # schema/unchanged: read-only, no Save, but still deletable.
            app.screen.query_one("#detail-delete-2", Button)

            # Confirm/cancel rows exist but start hidden.
            confirm = app.screen.query_one("#detail-confirm-row-0")
            assert not confirm.display

    asyncio.run(scenario())


def test_object_detail_edits_a_single_scalar_threshold(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)

            app.screen.query_one("#detail-value-1").value = "0.2"
            app.screen.query_one("#detail-save-1", Button).press()
            await pilot.pause()

        data = yaml.safe_load(cfg.read_text())
        null_rate_checks = [c for c in data["checks"] if c["metric"] == "null_rate"]
        assert null_rate_checks[0]["expect"] == {"max": 0.2}
        # the other checks are untouched
        row_count_checks = [c for c in data["checks"] if c["metric"] == "row_count"]
        assert row_count_checks[0]["expect"] == {"between": [1, 1000]}

    asyncio.run(scenario())


def test_object_detail_edit_does_not_change_check_id(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)

            app.screen.query_one("#detail-value-1").value = "0.2"
            app.screen.query_one("#detail-save-1", Button).press()
            await pilot.pause()

        config = load_config(cfg)
        matches = [c for c in config.checks if c.metric == "null_rate"]
        assert len(matches) == 1
        assert check_id(matches[0]) == check_id(_null_rate_check())

    asyncio.run(scenario())


def test_object_detail_edits_a_between_lo_hi_pair(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)

            app.screen.query_one("#detail-lo-0").value = "5"
            app.screen.query_one("#detail-hi-0").value = "2000"
            app.screen.query_one("#detail-save-0", Button).press()
            await pilot.pause()

        data = yaml.safe_load(cfg.read_text())
        row_count_checks = [c for c in data["checks"] if c["metric"] == "row_count"]
        assert row_count_checks[0]["expect"] == {"between": [5.0, 2000.0]}

    asyncio.run(scenario())


def test_object_detail_between_lo_greater_than_hi_is_rejected(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        original_text = cfg.read_text()

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)

            app.screen.query_one("#detail-lo-0").value = "500"
            app.screen.query_one("#detail-hi-0").value = "1"
            app.screen.query_one("#detail-save-0", Button).press()
            await pilot.pause()

            messages = [n.message for n in app._notifications]
            assert any("lo <= hi" in m for m in messages)

        assert cfg.read_text() == original_text

    asyncio.run(scenario())


def test_object_detail_invalid_scalar_value_notifies_and_keeps_disk(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        original_text = cfg.read_text()

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)

            app.screen.query_one("#detail-value-1").value = "not-a-number"
            app.screen.query_one("#detail-save-1", Button).press()
            await pilot.pause()

            messages = [n.message for n in app._notifications]
            assert any("not a number" in m for m in messages)

        assert cfg.read_text() == original_text

    asyncio.run(scenario())


def test_object_detail_delete_requires_a_second_press(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        original_text = cfg.read_text()

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)

            app.screen.query_one("#detail-delete-1", Button).press()
            await pilot.pause()

            # A single press only reveals the confirm row -- nothing is
            # deleted yet.
            assert app.screen.query_one("#detail-confirm-row-1").display
            assert cfg.read_text() == original_text

    asyncio.run(scenario())


def test_object_detail_delete_cancel_keeps_the_check(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        original_text = cfg.read_text()

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)

            app.screen.query_one("#detail-delete-1", Button).press()
            await pilot.pause()
            app.screen.query_one("#detail-cancel-1", Button).press()
            await pilot.pause()

            assert not app.screen.query_one("#detail-confirm-row-1").display

        assert cfg.read_text() == original_text

    asyncio.run(scenario())


def test_object_detail_delete_confirmed_removes_the_check_from_disk(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)

            app.screen.query_one("#detail-delete-1", Button).press()
            await pilot.pause()
            app.screen.query_one("#detail-confirm-1", Button).press()
            await pilot.pause()

        data = yaml.safe_load(cfg.read_text())
        metrics = {c["metric"] for c in data["checks"]}
        assert "null_rate" not in metrics
        assert metrics == {"row_count", "schema"}  # the other two survive

    asyncio.run(scenario())


def test_object_detail_delete_confirmed_refreshes_its_own_grid_and_edit_panel(
    tmp_path,
):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)

            table = app.screen.query_one(DataTable)
            assert table.row_count == 3

            app.screen.query_one("#detail-delete-1", Button).press()
            await pilot.pause()
            app.screen.query_one("#detail-confirm-1", Button).press()
            await pilot.pause()

            # Still on the same screen -- no need to pop back to Home and
            # drill in again to see the deletion reflected.
            assert isinstance(app.screen, ObjectDetailScreen)
            assert table.row_count == 2
            row_keys = {key.value for key in table.rows}
            assert check_id(_null_rate_check()) not in row_keys
            assert check_id(_row_count_check()) in row_keys
            assert check_id(_schema_check()) in row_keys

            # The edit panel's rows shifted down by one -- what was index 2
            # (schema) is now index 1.
            assert app.screen.query_one("#detail-delete-1", Button)

    asyncio.run(scenario())


def test_object_detail_dismiss_after_a_mutation_reloads_home_dashboard(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)

            app.screen.query_one("#detail-value-1").value = "0.2"
            app.screen.query_one("#detail-save-1", Button).press()
            await pilot.pause()

            await pilot.press("escape")
            await pilot.pause()

            assert not isinstance(app.screen, ObjectDetailScreen)

        # Home's own in-memory config reloaded the edited threshold.
        matches = [c for c in app.config.checks if c.metric == "null_rate"]
        assert matches[0].expect.operand == 0.2

    asyncio.run(scenario())


def test_object_detail_dismiss_after_delete_updates_home_grid(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#dashboard-grid", DataTable)
            assert _OBJECT_ROW_KEY in {key.value for key in table.rows}

            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ObjectDetailScreen)

            app.screen.query_one("#detail-delete-0", Button).press()
            await pilot.pause()
            app.screen.query_one("#detail-confirm-0", Button).press()
            await pilot.pause()
            app.screen.query_one("#detail-delete-0", Button).press()
            await pilot.pause()
            app.screen.query_one("#detail-confirm-0", Button).press()
            await pilot.pause()

            await pilot.press("escape")
            await pilot.pause()

            # Only schema is left for s.t -- Home's row for it survives,
            # rolled up from the one remaining check.
            table = app.query_one("#dashboard-grid", DataTable)
            assert _OBJECT_ROW_KEY in {key.value for key in table.rows}

        config = load_config(cfg)
        assert {c.metric for c in config.checks} == {"schema"}

    asyncio.run(scenario())


def test_object_detail_dismiss_without_any_mutation_does_not_reload(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)

            config_before = app.config
            await pilot.press("escape")
            await pilot.pause()

            # Nothing was edited or deleted -- Home's config object is the
            # exact same instance, not just an equal reload.
            assert app.config is config_before

    asyncio.run(scenario())
