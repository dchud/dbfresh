"""Pilot tests for editing and deleting checks from ObjectDetailScreen --
the connection-free counterpart to ConfigureScreen's own in-Propose
existing-check editing, reached straight from the Home grid's drill-in."""

import asyncio

import pytest
import yaml
from textual.css.query import NoMatches
from textual.widgets import Button, DataTable

from dbfresh.checks import Check, check_id
from dbfresh.config import load_config
from dbfresh.models import Status
from dbfresh.tui.app import DbfreshApp
from dbfresh.tui.screens import ObjectDetailScreen

_OBJECT_ROW_KEY = "s\x1ft"


def _overall_glyph(table, row_key):
    return table.get_cell(row_key, "overall").plain


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


def _vs_previous_ratio_config(path, db):
    """One row_count check with a full ratio guard (both bounds set) plus
    a max_delta guard alongside it -- the shape that must survive a
    ratio-only edit untouched."""
    path.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
        "checks:\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: row_count\n"
        "    expect:\n"
        "      vs_previous:\n"
        "        baseline: last_same_weekday\n"
        "        min_ratio: 0.75\n"
        "        max_ratio: 1.25\n"
        "        max_delta: 500\n"
        "        on_missing: warn\n"
    )
    return path


def _vs_previous_delta_only_config(path, db):
    """A vs_previous check guarded only by delta, not ratio -- out of
    scope for this screen's ratio-editing form, so it must stay
    read-only."""
    path.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
        "checks:\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: row_count\n"
        "    expect:\n"
        "      vs_previous:\n"
        "        baseline: previous\n"
        "        max_delta: 500\n"
    )
    return path


async def _open_object_detail(pilot):
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()
    assert isinstance(pilot.app.screen, ObjectDetailScreen)


def _two_object_config(path, db):
    """Two objects on one source -- "t" has a real (empty) table behind it
    (see _seed_db); "u" does not, so touching it during a run always
    errors. Used to prove a scoped run leaves an unrelated object
    untouched, the same way test_runner.py's own only= tests prove it for
    an unrelated source.
    """
    path.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
        "checks:\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: row_count\n"
        "    expect: { between: [0, 1000] }\n"
        "  - source: s\n"
        "    object: u\n"
        "    metric: row_count\n"
        "    expect: { between: [0, 1000] }\n"
    )
    return path


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
        null_rate_checks = [
            c for c in data["checks"] if c["metric"] == "null_rate"
        ]
        assert null_rate_checks[0]["expect"] == {"max": 0.2}
        # the other checks are untouched
        row_count_checks = [
            c for c in data["checks"] if c["metric"] == "row_count"
        ]
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
        row_count_checks = [
            c for c in data["checks"] if c["metric"] == "row_count"
        ]
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


def test_object_detail_edits_a_vs_previous_ratio_pair(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _vs_previous_ratio_config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)

            assert app.screen.query_one("#detail-lo-0").value == "0.75"
            assert app.screen.query_one("#detail-hi-0").value == "1.25"

            app.screen.query_one("#detail-lo-0").value = "0.5"
            app.screen.query_one("#detail-hi-0").value = "1.5"
            app.screen.query_one("#detail-save-0", Button).press()
            await pilot.pause()

        data = yaml.safe_load(cfg.read_text())
        expect = data["checks"][0]["expect"]["vs_previous"]
        assert expect["min_ratio"] == 0.5
        assert expect["max_ratio"] == 1.5
        # everything else about the guard survives untouched.
        assert expect["baseline"] == "last_same_weekday"
        assert expect["max_delta"] == 500
        assert expect["on_missing"] == "warn"

    asyncio.run(scenario())


def test_object_detail_vs_previous_ratio_min_greater_than_max_is_rejected(
    tmp_path,
):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _vs_previous_ratio_config(tmp_path / "config.yaml", db)
        original_text = cfg.read_text()

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)

            app.screen.query_one("#detail-lo-0").value = "2"
            app.screen.query_one("#detail-hi-0").value = "1"
            app.screen.query_one("#detail-save-0", Button).press()
            await pilot.pause()

            messages = [n.message for n in app._notifications]
            assert any("min <= max" in m for m in messages)

        assert cfg.read_text() == original_text

    asyncio.run(scenario())


def test_object_detail_vs_previous_ratio_non_numeric_is_rejected(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _vs_previous_ratio_config(tmp_path / "config.yaml", db)
        original_text = cfg.read_text()

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)

            app.screen.query_one("#detail-lo-0").value = "not-a-number"
            app.screen.query_one("#detail-save-0", Button).press()
            await pilot.pause()

            messages = [n.message for n in app._notifications]
            assert any("requires two numbers" in m for m in messages)

        assert cfg.read_text() == original_text

    asyncio.run(scenario())


def test_object_detail_vs_previous_delta_only_stays_read_only(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _vs_previous_delta_only_config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)

            with pytest.raises(NoMatches):
                app.screen.query_one("#detail-lo-0")
            with pytest.raises(NoMatches):
                app.screen.query_one("#detail-hi-0")
            with pytest.raises(NoMatches):
                app.screen.query_one("#detail-save-0")
            # still deletable, like every other read-only row.
            app.screen.query_one("#detail-delete-0", Button)

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


def test_object_detail_dismiss_after_a_mutation_reloads_home_dashboard(
    tmp_path,
):
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


# -- "Run this object" (object-scoped run) -----------------------------------


def test_object_detail_run_this_object_button_runs_only_this_objects_checks(
    tmp_path, pump_until
):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)  # creates table "t" only -- "u" has no table behind it
        cfg = _two_object_config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)  # drills into s.t (first row)
            detail_table = app.screen.query_one(DataTable)
            row_count_id = check_id(
                Check(source="s", object="t", metric="row_count")
            )
            assert (
                _overall_glyph(detail_table, row_count_id) == "·"
            )  # never observed

            app.screen.query_one("#detail-run-object-btn", Button).press()
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(pilot, lambda: app.last_run is not None)

            # Only "t" ran -- "u" (no real table behind it) was never even
            # touched, so the run stayed OK instead of ERROR.
            assert app.last_run is not None
            assert app.last_run.status == Status.OK
            assert [r.object for r in app.last_run.results] == ["t"]

            detail_table = app.screen.query_one(DataTable)
            assert _overall_glyph(detail_table, row_count_id) == "✓"

    asyncio.run(scenario())


def test_object_detail_run_this_object_also_refreshes_the_home_grid(
    tmp_path, pump_until
):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _two_object_config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)

            app.screen.query_one("#detail-run-object-btn", Button).press()
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot,
                lambda: (
                    _overall_glyph(
                        app.query_one("#dashboard-grid", DataTable), "s\x1ft"
                    )
                    == "✓"
                ),
            )

            # Home's own grid is a different, non-topmost screen -- still
            # picked up without popping back to it first.
            home_table = app.query_one("#dashboard-grid", DataTable)
            assert _overall_glyph(home_table, "s\x1ft") == "✓"
            assert _overall_glyph(home_table, "s\x1fu") == "·"  # untouched

    asyncio.run(scenario())


def test_object_detail_run_object_binding_matches_the_button(
    tmp_path, pump_until
):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _two_object_config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)

            await pilot.press("O")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(pilot, lambda: app.last_run is not None)

            assert app.last_run is not None
            assert [r.object for r in app.last_run.results] == ["t"]

    asyncio.run(scenario())


def test_global_run_from_object_detail_still_runs_every_object(
    tmp_path, pump_until
):
    """'r' (run everything) keeps working unscoped from ObjectDetailScreen
    -- scoping only ever kicks in via the new 'O' binding / button, never
    by way of the global run action."""

    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _two_object_config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)

            await pilot.press("r")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(pilot, lambda: app.last_run is not None)

            assert app.last_run is not None
            assert {r.object for r in app.last_run.results} == {"t", "u"}
            # "u" has no real table behind it, unlike the scoped-run tests
            # above -- touching it here is exactly the point.
            assert app.last_run.status == Status.ERROR

    asyncio.run(scenario())
