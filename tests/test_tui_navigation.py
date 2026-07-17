"""Cross-screen navigation hygiene: no duplicate screen stacking, footers
that only advertise keys that do something on the screen showing them, the
Enter-to-drill hint, the '?' help overlay, and manual config reload.
"""

from __future__ import annotations

import asyncio

from dbfresh.tui.app import DbfreshApp
from dbfresh.tui.configure import ConfigureScreen
from dbfresh.tui.screens import (
    HelpScreen,
    HistoryScreen,
    ObjectDetailScreen,
    ReportScreen,
    StoreScreen,
)


def _config(path, db):
    path.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
        "checks:\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: row_count\n"
        "    expect: { between: [1, 10] }\n"
    )
    return path


def _seed_db(path):
    from dbfresh.adapters.sqlite import SqliteAdapter

    adapter = SqliteAdapter(str(path))
    adapter.rows("CREATE TABLE t (id INTEGER)")
    adapter.rows("INSERT INTO t (id) VALUES (1)")
    adapter.close()


def _app(tmp_path):
    db = tmp_path / "data.db"
    _seed_db(db)
    cfg = _config(tmp_path / "config.yaml", db)
    return DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))


def _bound_keys(screen) -> set[str]:
    return set(screen.active_bindings)


# --- no duplicate stacking -------------------------------------------------


def test_pressing_report_repeatedly_does_not_stack_a_second_report_screen(tmp_path):
    async def scenario():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            assert isinstance(app.screen, ReportScreen)
            assert len(app.screen_stack) == 2

            await pilot.press("p")
            await pilot.press("p")
            await pilot.pause()

            assert isinstance(app.screen, ReportScreen)
            assert len(app.screen_stack) == 2

    asyncio.run(scenario())


def test_pressing_configure_or_store_while_report_is_open_does_not_navigate(tmp_path):
    """Once a screen is pushed on top of Home, 'c'/'p'/'s' are Home-only --
    none of them jump to a different destination without going back through
    Home first, so pressing 'c' or 's' while Report is open does nothing."""

    async def scenario():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            assert isinstance(app.screen, ReportScreen)

            await pilot.press("c")
            await pilot.press("s")
            await pilot.pause()

            assert isinstance(app.screen, ReportScreen)
            assert len(app.screen_stack) == 2

    asyncio.run(scenario())


def test_pressing_store_repeatedly_does_not_stack_a_second_store_screen(tmp_path):
    async def scenario():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("s")
            await pilot.press("s")
            await pilot.press("s")
            await pilot.pause()
            assert isinstance(app.screen, StoreScreen)
            assert len(app.screen_stack) == 2

    asyncio.run(scenario())


def test_pressing_configure_repeatedly_does_not_stack_a_second_configure_screen(
    tmp_path,
):
    async def scenario():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.press("c")
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigureScreen)
            assert len(app.screen_stack) == 2

    asyncio.run(scenario())


def test_repeated_enter_on_home_drills_down_without_stacking_duplicates(tmp_path):
    """Home's row -> ObjectDetail -> History drill-in, each step consuming
    exactly one Enter -- the screen stack grows by exactly one screen per
    keypress, never two, even with no pause between presses."""

    async def scenario():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ObjectDetailScreen)
            assert len(app.screen_stack) == 2

            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, HistoryScreen)
            assert len(app.screen_stack) == 3

    asyncio.run(scenario())


# --- per-screen footer scope -------------------------------------------------


def test_home_footer_shows_its_own_nav_keys_and_the_drill_in_hint(tmp_path):
    async def scenario():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            keys = _bound_keys(app.screen)
            assert {
                "r",
                "R",
                "c",
                "p",
                "s",
                "f",
                "slash",
                "question_mark",
                "q",
                "enter",
            } <= keys

    asyncio.run(scenario())


def test_pushed_screens_hide_home_only_nav_keys_from_the_footer(tmp_path):
    """Configure/Report/Store never show up on a screen other than Home --
    once any screen is pushed, only Run/Reload/Help/Quit (plus whatever
    that screen binds for itself) remain. The grid's own view controls
    (filter/search) are Home-only for the same reason."""

    async def scenario():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()

            for key, expect_type in (("p", ReportScreen), ("s", StoreScreen)):
                await pilot.press(key)
                await pilot.pause()
                assert isinstance(app.screen, expect_type)
                keys = _bound_keys(app.screen)
                assert not ({"c", "p", "s", "f", "slash"} & keys)
                assert {"r", "R", "question_mark", "q", "escape"} <= keys
                await pilot.press("escape")
                await pilot.pause()

            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigureScreen)
            keys = _bound_keys(app.screen)
            assert not ({"c", "p", "s", "f", "slash"} & keys)
            assert {"r", "R", "question_mark", "q", "escape"} <= keys

    asyncio.run(scenario())


def test_object_detail_footer_hides_home_keys_but_keeps_its_own_enter_hint(tmp_path):
    async def scenario():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ObjectDetailScreen)
            keys = _bound_keys(app.screen)
            assert not ({"c", "p", "s", "f", "slash"} & keys)
            assert {"enter", "escape", "r", "R", "O", "question_mark", "q"} <= keys

    asyncio.run(scenario())


def test_run_still_works_from_a_pushed_screen(tmp_path, pump_until):
    """Unlike configure/report/store, 'r' is not Home-only -- it never
    pushes a screen, so it has nothing to duplicate, and it stays available
    (and shown) everywhere. (The full re-run-refreshes-the-open-screen
    behavior is covered in test_tui_app.py; this only checks the binding
    itself is still reachable from a pushed screen.)"""

    async def scenario():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            assert isinstance(app.screen, ReportScreen)

            await pilot.press("r")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(pilot, lambda: app.last_run is not None)

            assert app.last_run is not None

    asyncio.run(scenario())


# --- escape label consistency ------------------------------------------------


def _escape_label(screen) -> str:
    return screen.active_bindings["escape"].binding.description


def test_escape_reads_back_on_navigation_screens_and_cancel_on_configure(tmp_path):
    async def scenario():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()

            for key, expect_type in (("p", ReportScreen), ("s", StoreScreen)):
                await pilot.press(key)
                await pilot.pause()
                assert isinstance(app.screen, expect_type)
                assert _escape_label(app.screen) == "Back"
                await pilot.press("escape")
                await pilot.pause()

            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ObjectDetailScreen)
            assert _escape_label(app.screen) == "Back"
            await pilot.press("escape")
            await pilot.pause()

            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigureScreen)
            assert _escape_label(app.screen) == "Cancel"

    asyncio.run(scenario())


# --- headings and Header title ------------------------------------------------


def test_pushed_screens_set_their_own_heading_and_header_title(tmp_path):
    async def scenario():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()

            cases = (("p", ReportScreen, "Report"), ("s", StoreScreen, "Store"))
            for key, expect_type, label in cases:
                await pilot.press(key)
                await pilot.pause()
                assert isinstance(app.screen, expect_type)
                assert app.screen.title == label
                heading = app.screen.query(".screen-heading").first()
                assert label in str(heading.render())
                await pilot.press("escape")
                await pilot.pause()

            await pilot.press("c")
            await pilot.pause()
            assert app.screen.title == "Configure"
            await pilot.press("escape")
            await pilot.pause()

            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ObjectDetailScreen)
            assert app.screen.title == "Object detail"
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, HistoryScreen)
            assert app.screen.title == "History"

    asyncio.run(scenario())


# --- help overlay --------------------------------------------------------------


def test_help_overlay_opens_from_home_with_bindings_and_status_legend(tmp_path):
    async def scenario():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("?")
            await pilot.pause()

            assert isinstance(app.screen, HelpScreen)
            bindings_text = str(app.screen.query_one("#help-bindings").render())
            assert "run checks" in bindings_text
            assert "reload config" in bindings_text
            assert "non-OK filter" in bindings_text
            assert "search by object" in bindings_text
            assert "run this object" in bindings_text
            legend_text = str(app.screen.query_one("#help-legend").render())
            assert "ok" in legend_text and "never observed" in legend_text

    asyncio.run(scenario())


def test_help_overlay_dismisses_with_escape_or_question_mark(tmp_path):
    async def scenario():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()

            await pilot.press("?")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, HelpScreen)

            await pilot.press("?")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            await pilot.press("?")
            await pilot.pause()
            assert not isinstance(app.screen, HelpScreen)

    asyncio.run(scenario())


def test_help_overlay_opens_over_a_pushed_screen_and_returns_to_it(tmp_path):
    async def scenario():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            assert isinstance(app.screen, ReportScreen)

            await pilot.press("?")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            assert len(app.screen_stack) == 3

            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, ReportScreen)
            assert len(app.screen_stack) == 2

    asyncio.run(scenario())


# --- manual config reload -----------------------------------------------------


def test_reload_config_picks_up_an_edit_made_outside_the_session(tmp_path):
    async def scenario():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#dashboard-grid")
            assert table.row_count == 2  # header("s") + s.t

            # Simulate an edit made in another window/editor while the TUI
            # is already running -- config is otherwise only ever (re)read
            # at mount time and right after a write this same session made.
            app.config_path.write_text(
                app.config_path.read_text()
                + "  - source: s\n    object: u\n    metric: row_count\n"
                "    expect: { between: [1, 10] }\n"
            )

            await pilot.press("R")
            await pilot.pause()

            table = app.query_one("#dashboard-grid")
            assert table.row_count == 3  # header("s") + s.t + s.u
            messages = [n.message for n in app._notifications]
            assert any("config reloaded" in m for m in messages)

    asyncio.run(scenario())


def test_reload_config_failure_is_caught_not_crashed(tmp_path):
    async def scenario():
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()

            app.config_path.write_text("not: [valid, yaml structure for dbfresh")

            await pilot.press("R")
            await pilot.pause()

            messages = [n.message for n in app._notifications]
            assert any("reload failed" in m.lower() for m in messages)
            # The app survived -- the dashboard is still queryable and
            # still reflects the last-known-good config.
            table = app.query_one("#dashboard-grid")
            assert table.row_count == 2  # header("s") + s.t

    asyncio.run(scenario())
