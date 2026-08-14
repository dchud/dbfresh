"""Pilot tests for ObjectDetailScreen: the check grid, the read-only
checks panel below it, and the object-scoped run affordance -- reached
straight from the Home grid's drill-in."""

import asyncio

from helpers import null_rate_check, overall_glyph, row_count_check
from textual.widgets import Button, DataTable, Input, Static

from dbfresh.checks import Check, check_id
from dbfresh.models import Result, Status
from dbfresh.store import Store
from dbfresh.tui.app import DbfreshApp, RunProgress
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


def _seed_observation(
    store, check, status, value=None, expected=None, error=None
):
    """One completed run with a single observation for ``check`` -- the
    minimum needed for Store.latest_observation(check_id(check)) to return
    it, mirroring test_tui_app.py's own ``_seed_status`` but carrying
    ``expected``/``error``/``value`` too, since the check-detail line reads
    all three.
    """
    run_id = store.start_run()
    store.record_observation(
        run_id,
        Result(
            object=check.object,
            metric=check.metric,
            status=status,
            source=check.source,
            value=value,
            expected=expected,
            error=error,
            check_id=check_id(check),
        ),
    )
    store.finish_run(run_id, status)


def _schema_check():
    return Check(source="s", object="t", metric="schema")


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


# -- read-only checks panel ----------------------------------------------


def test_object_detail_shows_config_path_and_check_identity(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)

            note = str(app.screen.query_one("#detail-checks-note").render())
            assert str(cfg) in note

            lines = [
                str(child.render())
                for child in app.screen.query_one(
                    "#detail-checks-list"
                ).children
            ]
            assert any(
                "row_count" in line and "between 1 and 1000" in line
                for line in lines
            )
            assert any(
                "null_rate (email)" in line and "max 0.05" in line
                for line in lines
            )
            assert any(
                "schema" in line and "unchanged" in line for line in lines
            )

    asyncio.run(scenario())


def test_object_detail_checks_panel_has_no_edit_affordances(tmp_path):
    """Every check's threshold and delete controls are gone -- config is
    edited by hand, so the panel below the grid is text only."""

    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)

            checks_list = app.screen.query_one("#detail-checks-list")
            assert not checks_list.query(Input)
            assert not checks_list.query(Button)

    asyncio.run(scenario())


def test_object_detail_dismiss_does_not_reload_config(tmp_path):
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

            # This screen never writes, so dismissing it never has anything
            # to reload -- Home's config object is the exact same instance,
            # not just an equal reload.
            assert app.config is config_before

    asyncio.run(scenario())


# -- inline check-detail line -------------------------------------------


def test_object_detail_highlighting_a_fail_check_shows_expected_and_observed(
    tmp_path,
):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"
        store = Store(store_path)
        _seed_observation(
            store,
            row_count_check(),
            Status.FAIL,
            value=5000,
            expected="between [1, 1000]",
        )
        store.close()

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)

            # Cursor starts on row 0 (row_count) -- no move needed.
            line = app.screen.query_one("#check-detail-line", Static)
            assert line.display
            text = str(line.content)
            assert "expected" in text
            assert "between [1, 1000]" in text
            assert "observed" in text
            assert "5000" in text

    asyncio.run(scenario())


def test_object_detail_highlighting_an_error_check_shows_the_error_message(
    tmp_path,
):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"
        store = Store(store_path)
        _seed_observation(
            store,
            null_rate_check(),
            Status.ERROR,
            error="connection refused",
        )
        store.close()

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)

            await pilot.press("down")  # row_count (0) -> null_rate (1)
            await pilot.pause()

            line = app.screen.query_one("#check-detail-line", Static)
            assert line.display
            text = str(line.content)
            assert "error:" in text
            assert "connection refused" in text

    asyncio.run(scenario())


def test_object_detail_highlighting_an_ok_check_hides_the_line(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"
        store = Store(store_path)
        _seed_observation(store, row_count_check(), Status.OK, value=3)
        store.close()

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)

            line = app.screen.query_one("#check-detail-line", Static)
            assert not line.display

    asyncio.run(scenario())


def test_object_detail_cursor_move_from_failing_to_ok_hides_the_line(
    tmp_path,
):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"
        store = Store(store_path)
        _seed_observation(
            store,
            row_count_check(),
            Status.FAIL,
            value=5000,
            expected="between [1, 1000]",
        )
        _seed_observation(store, null_rate_check(), Status.OK, value=0.01)
        store.close()

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)

            line = app.screen.query_one("#check-detail-line", Static)
            assert line.display  # row_count (FAIL) is highlighted first

            await pilot.press("down")  # row_count (0) -> null_rate (1), OK
            await pilot.pause()

            assert not line.display

    asyncio.run(scenario())


def test_object_detail_never_observed_check_hides_the_line(tmp_path):
    """No observation at all on this machine yet -- distinct from OK/SKIPPED,
    but hidden the same way (nothing to review)."""

    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)

            line = app.screen.query_one("#check-detail-line", Static)
            assert not line.display

    asyncio.run(scenario())


# -- "Run these checks" (object-scoped run) ----------------------------------


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
                overall_glyph(detail_table, row_count_id) == "·"
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
            assert overall_glyph(detail_table, row_count_id) == "✓"

    asyncio.run(scenario())


def test_object_detail_overall_cell_updates_live_via_apply_live_result(
    tmp_path,
):
    """Asserted at the handler level -- calling apply_live_result directly
    with a synthetic Result -- rather than racing the worker thread for a
    genuine mid-run snapshot, which is flaky.

    The check's own row flips the instant its result arrives; an
    unrelated check on the same object, with no result yet, is
    untouched."""

    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)
            detail_table = app.screen.query_one(DataTable)
            null_rate_id = check_id(null_rate_check())
            row_count_id = check_id(row_count_check())
            assert overall_glyph(detail_table, null_rate_id) == "·"

            app.screen.apply_live_result(
                Result(
                    object="t",
                    metric="null_rate",
                    status=Status.FAIL,
                    source="s",
                    check_id=null_rate_id,
                )
            )
            await pilot.pause()

            assert overall_glyph(detail_table, null_rate_id) == "✗"
            assert overall_glyph(detail_table, row_count_id) == "·"

    asyncio.run(scenario())


def test_object_detail_apply_live_result_ignores_a_different_objects_check(
    tmp_path,
):
    """A full run touches every object, not just the one this screen is
    showing -- a result for a different source/object must never reach
    into this screen's grid (there is no row for it to land on)."""

    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _two_object_config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)  # drills into s.t
            detail_table = app.screen.query_one(DataTable)
            row_count_id = check_id(
                Check(source="s", object="t", metric="row_count")
            )
            assert overall_glyph(detail_table, row_count_id) == "·"

            app.screen.apply_live_result(
                Result(
                    object="u",
                    metric="row_count",
                    status=Status.OK,
                    source="s",
                    check_id=check_id(
                        Check(source="s", object="u", metric="row_count")
                    ),
                )
            )
            await pilot.pause()

            assert overall_glyph(detail_table, row_count_id) == "·"

    asyncio.run(scenario())


def test_object_detail_live_update_flashes_the_overall_cell(tmp_path):
    """apply_live_result's overall-cell write carries the flash_cell
    highlight background immediately, not just the plain status glyph."""
    from dbfresh.tui.dashboard import HIGHLIGHT_BG, _status_cell

    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)
            detail_table = app.screen.query_one(DataTable)
            null_rate_id = check_id(null_rate_check())

            app.screen.apply_live_result(
                Result(
                    object="t",
                    metric="null_rate",
                    status=Status.FAIL,
                    source="s",
                    check_id=null_rate_id,
                )
            )
            await pilot.pause()

            expected = _status_cell(Status.FAIL)
            expected.stylize(f"on {HIGHLIGHT_BG}")
            assert detail_table.get_cell(null_rate_id, "overall") == expected
            assert detail_table.get_cell(null_rate_id, "overall").plain == "✗"

    asyncio.run(scenario())


def test_object_detail_live_update_highlight_clears_after_the_delay(
    tmp_path, monkeypatch
):
    """Once flash_cell's delay has elapsed, the check's overall cell reads
    exactly as the plain status cell again."""
    from dbfresh.tui.dashboard import _status_cell

    # The highlight assertion below runs before the clear is due, so the
    # delay is the margin it has to beat. 0.05 left ~50ms and CI missed
    # it; 0.5 leaves ~500ms, and the settle assertion just waits past it.
    monkeypatch.setattr("dbfresh.tui.dashboard.DEFAULT_FLASH_DELAY", 0.5)

    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)
            detail_table = app.screen.query_one(DataTable)
            null_rate_id = check_id(null_rate_check())

            app.screen.apply_live_result(
                Result(
                    object="t",
                    metric="null_rate",
                    status=Status.FAIL,
                    source="s",
                    check_id=null_rate_id,
                )
            )
            await pilot.pause()
            assert detail_table.get_cell(
                null_rate_id, "overall"
            ) != _status_cell(Status.FAIL)

            await pilot.pause(0.7)  # past the injected 0.5s delay

            assert detail_table.get_cell(
                null_rate_id, "overall"
            ) == _status_cell(Status.FAIL)

    asyncio.run(scenario())


def test_object_detail_re_flash_cancels_the_stale_clear(tmp_path, monkeypatch):
    """A second live update to the same check within the flash window must
    not let the first update's clear fire later and briefly revert the
    cell to the older, now-stale status."""
    from dbfresh.tui.dashboard import _status_cell

    # A generous delay, not the 0.05 the settle-only tests use. The
    # assertion below has to land inside the gap between the cancelled
    # clear and the rescheduled one, and pilot.pause is a floor rather
    # than an exact wait -- a loaded machine overshoots it. At 0.05 that
    # gap was ~15ms wide and CI overshot it; at 0.5 it is ~350ms, which
    # only a severe stall would miss. Waiting is one-directional (a pause
    # never returns early), so the margin only has to cover lateness.
    monkeypatch.setattr("dbfresh.tui.dashboard.DEFAULT_FLASH_DELAY", 0.5)

    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)
            detail_table = app.screen.query_one(DataTable)
            null_rate_id = check_id(null_rate_check())
            flash_key = (null_rate_id, "overall")

            # t=0: fails -- clear due at t=0.5.
            app.screen.apply_live_result(
                Result(
                    object="t",
                    metric="null_rate",
                    status=Status.FAIL,
                    source="s",
                    check_id=null_rate_id,
                )
            )
            await pilot.pause(0.4)  # t=0.4, still before the first clear
            first_timer = app.screen._cell_flash_timers[flash_key]

            # t=0.4: re-evaluated as ok (e.g. re-run) -- must cancel the
            # first clear and reschedule its own for t=0.9.
            app.screen.apply_live_result(
                Result(
                    object="t",
                    metric="null_rate",
                    status=Status.OK,
                    source="s",
                    check_id=null_rate_id,
                )
            )
            # Clock-free half of the invariant: the pending clear was
            # replaced rather than left to fire alongside a second one.
            assert app.screen._cell_flash_timers[flash_key] is not first_timer

            await pilot.pause(0.15)  # t=0.55: past the stale 0.5 deadline,
            # well before the real one at 0.9 -- a live stale clear would
            # have reverted this to the first (FAIL) status by now.
            assert overall_glyph(detail_table, null_rate_id) == "✓"
            assert detail_table.get_cell(
                null_rate_id, "overall"
            ) != _status_cell(Status.OK)  # still highlighted -- not settled

            await pilot.pause(0.5)  # t=1.05: past the real clear at 0.9

            assert overall_glyph(detail_table, null_rate_id) == "✓"
            assert detail_table.get_cell(
                null_rate_id, "overall"
            ) == _status_cell(Status.OK)

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
                    overall_glyph(
                        app.query_one("#dashboard-grid", DataTable), "s\x1ft"
                    )
                    == "✓"
                ),
            )

            # Home's own grid is a different, non-topmost screen -- still
            # picked up without popping back to it first.
            home_table = app.query_one("#dashboard-grid", DataTable)
            assert overall_glyph(home_table, "s\x1ft") == "✓"
            assert overall_glyph(home_table, "s\x1fu") == "·"  # untouched

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


def test_object_detail_run_label_is_plural_for_multiple_checks(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)  # three checks on t

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)

            button = app.screen.query_one("#detail-run-object-btn", Button)
            assert str(button.label) == "Run these checks"
            assert (
                app.screen.active_bindings["O"].binding.description
                == "Run these checks"
            )

    asyncio.run(scenario())


def test_object_detail_run_label_is_singular_for_one_check(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
            "checks:\n"
            "  - source: s\n"
            "    object: t\n"
            "    metric: row_count\n"
            "    expect: { between: [1, 1000] }\n"
        )

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await _open_object_detail(pilot)

            button = app.screen.query_one("#detail-run-object-btn", Button)
            assert str(button.label) == "Run this check"
            assert (
                app.screen.active_bindings["O"].binding.description
                == "Run this check"
            )

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


def test_object_detail_opened_mid_run_shows_results_that_already_arrived(
    tmp_path,
):
    """Drilling into an object mid-run must show the results this run has
    already produced for it.

    Observations are persisted in one batch once the whole run finishes
    (runner.run_and_persist), so the store this screen builds from holds
    nothing yet -- and apply_live_result only fires for results arriving
    after the screen is already on top. Without seeding from the app's
    results-so-far, the very failure that prompted the drill-in reads as
    never-observed until the entire run ends.
    """

    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            row_count_id = check_id(row_count_check())

            # A run is underway and this object's row_count has failed --
            # the X the user sees on Home before drilling in.
            app.on_run_progress(
                RunProgress(
                    1,
                    3,
                    result=Result(
                        object="t",
                        metric="row_count",
                        status=Status.FAIL,
                        source="s",
                        check_id=row_count_id,
                    ),
                )
            )
            await pilot.pause()

            await _open_object_detail(pilot)
            detail_table = app.screen.query_one(DataTable)
            assert overall_glyph(detail_table, row_count_id) == "✗"

    asyncio.run(scenario())


def test_object_detail_opened_mid_run_leaves_pending_checks_unobserved(
    tmp_path,
):
    """Seeding covers only the checks this run has actually returned. A
    check still in flight has no status yet and must keep reading
    never-observed rather than borrowing a sibling's."""

    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()

            app.on_run_progress(
                RunProgress(
                    1,
                    3,
                    result=Result(
                        object="t",
                        metric="row_count",
                        status=Status.FAIL,
                        source="s",
                        check_id=check_id(row_count_check()),
                    ),
                )
            )
            await pilot.pause()

            await _open_object_detail(pilot)
            detail_table = app.screen.query_one(DataTable)
            assert (
                overall_glyph(detail_table, check_id(row_count_check())) == "✗"
            )
            assert (
                overall_glyph(detail_table, check_id(null_rate_check())) == "·"
            )
            assert (
                overall_glyph(detail_table, check_id(_schema_check())) == "·"
            )

    asyncio.run(scenario())


def test_object_detail_seeding_ignores_another_objects_live_result(tmp_path):
    """A run evaluates every object, so the app's results-so-far map holds
    checks this screen has no row for. Seeding must be driven by this
    object's own rows, never by iterating the map and writing cells."""

    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _two_object_config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()

            # A result for object "u" -- no row for it on t's detail screen.
            app.on_run_progress(
                RunProgress(
                    1,
                    2,
                    result=Result(
                        object="u",
                        metric="row_count",
                        status=Status.FAIL,
                        source="s",
                        check_id=check_id(
                            Check(source="s", object="u", metric="row_count")
                        ),
                    ),
                )
            )
            await pilot.pause()

            await _open_object_detail(pilot)
            detail_table = app.screen.query_one(DataTable)
            row_count_id = check_id(
                Check(source="s", object="t", metric="row_count")
            )
            assert overall_glyph(detail_table, row_count_id) == "·"

    asyncio.run(scenario())
