"""The dbfresh ui Textual application.

A presentation layer only: the Home dashboard, Run action, and Configure /
Report / History destinations all read and write through the same
config/store/engine/configurator modules the CLI uses. No check semantics
live here.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.coordinate import Coordinate
from textual.message import Message
from textual.notifications import SeverityLevel
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    ProgressBar,
    Static,
)
from textual.worker import Worker, WorkerState

from dbfresh.checks import check_id
from dbfresh.config import Config, StoreConfig, load_config_tolerant
from dbfresh.env_hygiene import committable_env_file
from dbfresh.models import Result, RunResult, Status
from dbfresh.store import Store, resolve_store_path
from dbfresh.tui.dashboard import (
    DrillDownTable,
    GridRow,
    GridView,
    _day_cell,
    _status_cell,
    _worst_or_unknown,
    is_header_key,
    last_run_line,
    object_rows,
    populate_grid,
    status_legend,
    unobserved_count,
    unobserved_summary,
)

_GRID_ID = "dashboard-grid"
_RUN_PROGRESS_ID = "run-progress"
_RUN_WORKER_GROUP = "run-checks"
_SEARCH_INPUT_ID = "grid-search"

# configure/report/store push a screen on top of Home -- pressing the same
# key again (or one of the other two) while that screen is still open would
# either stack a second copy of it or jump to a different destination
# without going back through Home first, so all three only fire (and only
# show in the footer -- see DbfreshApp.check_action) while Home is the one
# and only screen on the stack. 'r' (run) and reload aren't here: neither
# ever pushes a screen, so neither has anything to stack, and both are
# useful from anywhere -- e.g. re-running while a Report is on top refreshes
# that same Report in place (see _refresh_topmost_screen).
#
# The grid view controls (filter/search) join them for a different reason:
# they act on Home's own grid and search box specifically, and
# App.query_one always resolves against the default screen (see
# _refresh_topmost_screen's own note on this) -- so left enabled, one of
# these firing while a different screen is on top would reach straight
# through to Home's hidden, off-screen widgets instead of doing nothing.
_HOME_ONLY_ACTIONS = frozenset(
    {
        "configure",
        "report",
        "store",
        "toggle_non_ok_filter",
        "toggle_search",
        "close_search",
    }
)

# Toast label and severity for a completed run's status counts, in the same
# worst-to-least-severe order the status legend reads -- a zero count is
# omitted rather than padding the summary with "0 skipped" noise.
_RUN_STATUS_LABEL: dict[Status, str] = {
    Status.OK: "ok",
    Status.WARN: "warned",
    Status.FAIL: "failed",
    Status.ERROR: "unreachable",
    Status.SKIPPED: "skipped",
}
_RUN_TOAST_SEVERITY: dict[Status, SeverityLevel] = {
    Status.OK: "information",
    Status.WARN: "warning",
    Status.FAIL: "error",
    Status.ERROR: "error",
    Status.SKIPPED: "information",
}

# Shown instead of the grid when there's nothing to show -- a zero-check
# config renders as just the grid's own header row otherwise, which reads
# as broken rather than as "nothing configured yet".
_EMPTY_STATE_MESSAGE = "no checks configured yet -- press 'c' to configure a source and its checks"

# Shown instead of the grid when checks exist but the active filter/search
# narrows them to nothing -- distinct from _EMPTY_STATE_MESSAGE above so a
# filtered-to-empty grid never reads as "nothing configured" (which would
# send the user toward Configure for a problem 'f'/'/' already explains).
_NO_MATCHING_ROWS_MESSAGE = "no rows match the current filter or search"

_MISSING_SECRETS_ID = "missing-secrets-banner"
# Its own glyph, not dashboard.status_glyph(Status.WARN) -- this banner
# flags a config problem (an unset secret), never a check result, so it
# is never built from the same lookup that renders OK/WARN/FAIL/ERROR/
# SKIPPED on the grid, even where the character happens to coincide.
_MISSING_SECRETS_GLYPH = "!"

_ENV_HYGIENE_ID = "env-hygiene-banner"


def _run_summary(run: RunResult) -> str:
    """A short "N ok, N failed, ..." toast summary of ``run``'s counts."""
    counts = dict.fromkeys(Status, 0)
    for result in run.results:
        counts[result.status] += 1
    parts = [
        f"{counts[status]} {label}"
        for status, label in _RUN_STATUS_LABEL.items()
        if counts[status]
    ]
    return ", ".join(parts) if parts else "no checks ran"


class RunProgress(Message):
    """One check finished during an active run.

    ``run_checks`` evaluates each source's checks on its own worker thread
    (see ``dbfresh.engine.run_checks``) and calls ``on_result`` from
    whichever of those threads just finished a check -- never the single
    Textual thread worker that calls ``run_and_persist``. ``post_message``
    is safe to call from any thread, unlike setting a reactive attribute or
    touching a widget directly, so the progress callback posts one of
    these per check instead of updating the header itself.

    ``result`` carries that just-completed check's own ``Result`` --
    ``on_run_progress`` uses it to flip that check's glyph on whichever
    grid(s) show it live, in addition to advancing the count/total in the
    header. It is not persisted yet (``run_and_persist`` writes
    observations only once the whole run finishes), so it is the only
    source of this run's results until then.
    """

    def __init__(
        self, count: int, total: int, result: Result | None = None
    ) -> None:
        self.count = count
        self.total = total
        self.result = result
        super().__init__()


class DbfreshApp(App):
    """Status dashboard over ``config_path``'s checks and ``store_path``."""

    TITLE = "dbfresh"
    CSS_PATH = "app.tcss"

    BINDINGS = [
        Binding("r", "run_checks", "Run"),
        Binding("R", "reload_config", "Reload"),
        Binding("c", "configure", "Configure"),
        Binding("p", "report", "Report"),
        Binding("s", "store", "Store"),
        Binding("f", "toggle_non_ok_filter", "Non-OK"),
        Binding("slash", "toggle_search", "Search"),
        # Only meaningful while the search box is open (see
        # action_close_search) -- not shown in the footer so it doesn't
        # read as globally available when it isn't.
        Binding("escape", "close_search", "Close search", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        config_path: str | Path,
        store_path: str | None = None,
        initial_config: Config | None = None,
        missing_secrets: Iterable[str] | None = None,
    ) -> None:
        """Build the app; ``initial_config``, when given, is used as-is at
        mount time instead of re-parsing ``config_path``.

        ``dbfresh ui`` (``cli._ui_command``) already parses the config once
        to fail cleanly before the Textual session ever starts; passing
        that same :class:`~dbfresh.config.Config` through here avoids
        parsing the same unchanged file a second time. Omit it (the
        default) to have :meth:`on_mount` load it itself -- what every
        test that constructs ``DbfreshApp`` directly relies on.

        ``missing_secrets`` names every ``${VAR}`` the config referenced
        but couldn't resolve (``cli._ui_command`` loads tolerantly rather
        than refusing to launch) -- shown as a banner on Home rather than
        acted on here; a source whose params still hold a literal
        ``${VAR}`` simply comes back ERROR the first time it's run.
        """
        super().__init__()
        # Textual bundles this theme (Catppuccin's own Macchiato hexes for
        # base/surface0/surface1/green/yellow/red/mauve/peach) -- app.tcss
        # (CSS_PATH above) fills in the rest of the named palette as extra
        # $-variables for this file's own rules to use.
        self.theme = "catppuccin-macchiato"
        self.config_path = Path(config_path)
        self._store_path_override = store_path
        self.config: Config | None = initial_config
        self.store: Store | None = None
        self.last_run: RunResult | None = None
        self._rows_by_key: dict[str, GridRow] = {}
        # This run's results so far, keyed by check_id -- reset each time a
        # new run starts (see on_worker_state_changed's RUNNING branch) and
        # filled in as RunProgress messages arrive (on_run_progress), so a
        # live grid update can roll up an object's checks from what this
        # run has produced without waiting for run_and_persist's end-of-run
        # write to the store.
        self._live_results: dict[str, Result] = {}
        self._view = GridView()
        self.missing_secrets: tuple[str, ...] = tuple(
            sorted(set(missing_secrets or ()))
        )
        # Computed once here rather than on every refresh_dashboard call --
        # the check runs git. _reload_config recomputes it, since that's
        # the only point after startup where the .env/gitignore state could
        # have changed.
        self._env_at_risk: Path | None = committable_env_file(self.config_path)

    def _missing_secrets_text(self) -> str:
        names = ", ".join(self.missing_secrets)
        return (
            f"{_MISSING_SECRETS_GLYPH} secrets not set: {names} -- set them in a "
            f".env file beside {self.config_path.name}, or export them"
        )

    def _env_hygiene_text(self) -> str:
        return (
            f".env beside {self.config_path.name} is not gitignored; it "
            f"likely holds secrets -- add it to .gitignore before committing."
        )

    def compose(self) -> ComposeResult:
        yield Header()
        banner = Static(self._missing_secrets_text(), id=_MISSING_SECRETS_ID)
        banner.display = bool(self.missing_secrets)
        yield banner
        env_hygiene_banner = Static(
            self._env_hygiene_text(), id=_ENV_HYGIENE_ID
        )
        env_hygiene_banner.display = self._env_at_risk is not None
        yield env_hygiene_banner
        # Out of the way (display hidden) until '/' reveals it
        # (action_toggle_search); populated from self._view on reopen so
        # it reflects an already-active search rather than always starting
        # blank.
        search_input = Input(
            value=self._view.search,
            placeholder="search source.object…",
            id=_SEARCH_INPUT_ID,
        )
        search_input.display = False
        # display alone doesn't drop it from the focus chain (Widget.
        # focusable checks the "visibility" rule, not "display") -- without
        # this, Textual's default auto-focus-on-mount would land on this
        # hidden Input instead of the grid, since it's earlier in the DOM.
        search_input.can_focus = False
        yield search_input
        yield DrillDownTable(
            id=_GRID_ID,
            cursor_type="row",
            zebra_stripes=True,
            cell_padding=2,
            # A status cell's own Rich style (status_style) sets its own
            # foreground -- "renderable" lets that survive on the cursor
            # row instead of the cursor's CSS color forcing every cell on
            # the selected row to one flat color, which would erase the
            # OK/WARN/FAIL/... encoding at exactly the row under focus.
            cursor_foreground_priority="renderable",
        )
        yield Static(status_legend(), id="status-legend")
        yield Static("", id="view-status")
        # Fills as a run's checks complete (on_run_progress); hidden here
        # so nothing shows while idle -- on_worker_state_changed's RUNNING
        # branch reveals it and resets it to 0 when a run starts, and its
        # SUCCESS/CANCELLED/ERROR branches hide it again once the run
        # finishes. The subtitle above already carries the "N/total" words
        # (see on_run_progress), so the bar shows percentage, not a second
        # copy of that count.
        progress_bar = ProgressBar(
            id=_RUN_PROGRESS_ID, show_percentage=True, show_eta=False
        )
        progress_bar.display = False
        yield progress_bar
        yield Static("", id="last-run-line")
        yield Static("", id="unobserved-line")
        yield Static(_EMPTY_STATE_MESSAGE, id="empty-state")
        yield Footer()

    def check_action(
        self, action: str, parameters: tuple[object, ...]
    ) -> bool | None:
        """Disable (and, per Textual's own Footer, hide) every Home-only
        action -- configure/report/store, plus the grid's filter/search
        controls -- while a screen is already pushed on top of Home; see
        :data:`_HOME_ONLY_ACTIONS`. Every other action (run, reload, help,
        quit, and whatever the topmost screen binds for itself) is
        unaffected -- this only ever narrows that one set.
        """
        return not (
            action in _HOME_ONLY_ACTIONS and len(self.screen_stack) > 1
        )

    def on_mount(self) -> None:
        if self.config is None:
            self._reload_config()
        self._open_store()
        self.refresh_dashboard()

    def _reload_config(self) -> None:
        # Tolerant, like cli._ui_command's initial load: a reload (after a
        # Configure write, or the no-initial-config path) keeps working
        # when a ${VAR} secret is still unset, refreshing missing_secrets
        # for the banner rather than raising and leaving the dashboard
        # stuck behind a "reload failed" toast.
        self.config, missing = load_config_tolerant(self.config_path)
        self.missing_secrets = tuple(sorted(missing))
        self._env_at_risk = committable_env_file(self.config_path)

    def action_reload_config(self) -> None:
        """Re-read ``config_path`` from disk on demand.

        Config is otherwise only ever (re)loaded at mount time and right
        after a write this same session made (Configure's Accept, an
        ObjectDetail edit/delete) -- an edit made in another window or by
        hand is never picked up without this. A distinct key from 'r'
        (Run) deliberately -- the two are easy to confuse by feel, and
        this one never touches the store or starts a worker.
        """
        try:
            self._reload_config()
        except Exception as exc:
            self.notify(
                f"config reload failed: {exc}",
                title="Reload failed",
                severity="error",
                timeout=10,
            )
            return
        self.refresh_dashboard()
        pending = unobserved_count(
            self._require_config().checks, self._require_store()
        )
        message = "config reloaded"
        if pending:
            message = f"{message} · {unobserved_summary(pending)}"
        self.notify(message)

    def action_help(self) -> None:
        from dbfresh.tui.screens import HelpScreen

        self.push_screen(HelpScreen())

    def _require_config(self) -> Config:
        """``self.config``, guaranteed set: every caller runs after ``on_mount``."""
        assert self.config is not None
        return self.config

    def _require_store(self) -> Store:
        """``self.store``, guaranteed set: every caller runs after ``on_mount``."""
        assert self.store is not None
        return self.store

    def _open_store(self) -> None:
        config = self._require_config()
        store_path = resolve_store_path(
            config_dir=config.config_dir,
            store_config=config.store,
            cli_store=self._store_path_override,
            env_store=os.environ.get("DBFRESH_STORE"),
        )
        self.store = Store(store_path)

    def refresh_dashboard(self) -> None:
        """Rebuild the dashboard grid from the current config, store, and
        active view controls (:attr:`_view`) -- the one place all three
        (a run finishing, a config reload, and every filter/search toggle
        below) funnel through, so the grid always reflects the view the
        user last set instead of silently resetting it.

        A config with no checks (or no sources to hang any on) has no rows
        at all -- rather than showing the grid's bare header row, swap in
        an empty-state hint pointing at Configure. Checks exist but the
        active filter/search narrows them to nothing is a different,
        equally-empty-looking case with a different fix (relax the view,
        not go configure something) -- see :data:`_NO_MATCHING_ROWS_MESSAGE`.
        """
        from dbfresh.report import display_timezone

        table = self.query_one(f"#{_GRID_ID}", DataTable)
        config = self._require_config()
        store = self._require_store()
        tz = display_timezone(config.calendar)
        today = datetime.now(tz).date()
        rows = object_rows(config, store, today, tz)
        visible = self._view.apply(rows)
        populate_grid(
            table, visible, today, label_header="object", group_headers=True
        )
        self._rows_by_key = {row.key: row for row in visible}
        self._skip_leading_header_cursor(table)

        if not rows:
            message: str | None = _EMPTY_STATE_MESSAGE
        elif not visible:
            message = _NO_MATCHING_ROWS_MESSAGE
        else:
            message = None
        empty = message is not None
        table.display = not empty
        self.query_one("#status-legend", Static).display = not empty
        empty_widget = self.query_one("#empty-state", Static)
        empty_widget.update(message or "")
        empty_widget.display = empty

        view_status = self.query_one("#view-status", Static)
        view_status.update(self._view.status_text())
        view_status.display = self._view.active

        last_run_widget = self.query_one("#last-run-line", Static)
        line = last_run_line(store, tz)
        last_run_widget.update(line or "")
        last_run_widget.display = line is not None

        unobserved_widget = self.query_one("#unobserved-line", Static)
        pending = unobserved_count(config.checks, store)
        unobserved_widget.update(
            unobserved_summary(pending) if pending else ""
        )
        unobserved_widget.display = pending > 0

        banner = self.query_one(f"#{_MISSING_SECRETS_ID}", Static)
        banner.update(self._missing_secrets_text())
        banner.display = bool(self.missing_secrets)

        env_hygiene_banner = self.query_one(f"#{_ENV_HYGIENE_ID}", Static)
        env_hygiene_banner.update(self._env_hygiene_text())
        env_hygiene_banner.display = self._env_at_risk is not None

    def _skip_leading_header_cursor(self, table: DataTable) -> None:
        """After a grouped (re)populate, row 0 is always a source header --
        ``DataTable.clear`` resets the cursor to ``(0, 0)`` and
        ``populate_grid`` never moves it, so left alone it would start
        parked on a label instead of a selectable object row. Advances the
        cursor to the first object row when that's the case; a no-op once
        the grid already has a non-header row under the cursor, and when
        the grid has no rows at all.

        ``DrillDownTable``'s own cursor-skip only fires on the up/down
        actions themselves, not on this programmatic reset, so the initial
        position needs this separate nudge.
        """
        if table.row_count == 0:
            return
        leading_key = table.coordinate_to_cell_key(
            Coordinate(0, 0)
        ).row_key.value
        if not is_header_key(leading_key):
            return
        for row_index in range(1, table.row_count):
            key = table.coordinate_to_cell_key(
                Coordinate(row_index, 0)
            ).row_key.value
            if not is_header_key(key):
                table.cursor_coordinate = Coordinate(row_index, 0)
                return

    # -- Home grid view controls: filter / search ---------------------------
    #
    # Both toggle a field on self._view and call refresh_dashboard(), which
    # funnels the whole rebuild -- rows, grid, empty-state, and the
    # indicator below -- through GridView.apply() in one place (see its own
    # docstring). Each is Home-only (see _HOME_ONLY_ACTIONS / check_action).

    def action_toggle_non_ok_filter(self) -> None:
        self._view.hide_ok = not self._view.hide_ok
        self.refresh_dashboard()

    def action_toggle_search(self) -> None:
        """Reveal (and focus) the label substring-search box. Safe to press
        again while it's already open -- just refocuses it."""
        search_input = self.query_one(f"#{_SEARCH_INPUT_ID}", Input)
        search_input.display = True
        search_input.can_focus = True
        search_input.focus()

    def action_close_search(self) -> None:
        """Escape while the search box is open: clear the search and hide
        the box again, returning focus to the grid. A no-op when the box
        isn't open, so binding this key globally on Home never does
        anything surprising the rest of the time.

        Unlike Enter (Input.Submitted, below), which leaves whatever was
        typed in place -- this is the "cancel" of the pair, not just a
        second way to close.
        """
        search_input = self.query_one(f"#{_SEARCH_INPUT_ID}", Input)
        if not search_input.display:
            return
        search_input.value = ""
        self._close_search(search_input)

    def _close_search(self, search_input: Input) -> None:
        search_input.display = False
        search_input.can_focus = False  # see compose()'s note on this
        self.query_one(f"#{_GRID_ID}", DataTable).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Live-filter the grid as the search box's text changes --
        clearing it back to empty naturally restores every row, since
        GridView.apply's own substring check is skipped for an empty
        needle."""
        if event.input.id != _SEARCH_INPUT_ID:
            return
        self._view.search = event.value
        self.refresh_dashboard()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the search box: commit the current search (already
        applied live by on_input_changed) and hide the box, same as
        action_close_search except the typed text is kept rather than
        cleared."""
        if event.input.id != _SEARCH_INPUT_ID:
            return
        self._close_search(event.input)

    def action_run_checks(self) -> None:
        """Start a check run in a worker thread; the UI stays responsive."""
        self._run_checks_worker()

    def run_object_checks(self, source: str, object_: str) -> None:
        """Start a check run scoped to one object's checks -- the
        ObjectDetailScreen affordance for running only what it's showing.

        Shares :meth:`_run_checks_worker`'s exclusive worker group with a
        full run, so the two can never write concurrently: either cancels
        the other exactly like two overlapping full runs already do (see
        ``on_worker_state_changed``), and a scoped run's completion is
        picked up by that same handler -- dashboard refresh, whichever
        screen is on top refreshing in place, and the completion toast --
        with no separate copy of that plumbing needed here.
        """
        self._run_checks_worker(only=source, object_=object_)

    @work(
        thread=True,
        exclusive=True,
        group=_RUN_WORKER_GROUP,
        exit_on_error=False,
    )
    def _run_checks_worker(
        self, only: str | None = None, object_: str | None = None
    ) -> RunResult:
        """Run every check (or, when ``only``/``object_`` are given, just
        those scoped to that source/object -- see
        ``dbfresh.runner.filter_checks``), posting a :class:`RunProgress`
        message per completed one along the way.

        ``on_result`` (see ``dbfresh.runner.run_and_persist`` /
        ``dbfresh.engine.run_checks``) fires from whichever per-source
        worker thread just finished a check, potentially several at once
        -- ``count`` is only ever mutated under ``lock``, and every update
        reaches the UI via ``post_message`` rather than by touching a
        widget or reactive attribute from these threads directly.
        """
        from dbfresh.runner import filter_checks, run_and_persist

        config = self._require_config()
        total = len(filter_checks(config.checks, only, object_))
        lock = threading.Lock()
        count = 0

        def on_result(result: Result) -> None:
            nonlocal count
            with lock:
                count += 1
                current = count
            self.post_message(RunProgress(current, total, result=result))

        # Persist through a fresh Store on this worker thread rather than the
        # app's shared self.store. on_unmount closes self.store from the main
        # thread; writing here on another thread would otherwise race that
        # close() on a single sqlite3 connection -- which segfaults the SQLite
        # C extension rather than raising. A separate connection to the same
        # file is WAL-safe (the same approach StoreScreen's prune worker uses),
        # and it keeps self.store touched only by the main thread.
        store = Store(self._require_store().path)
        try:
            return run_and_persist(
                config, store, only=only, object_=object_, on_result=on_result
            )
        finally:
            store.close()

    def _run_progress_bar(self) -> ProgressBar:
        return self.query_one(f"#{_RUN_PROGRESS_ID}", ProgressBar)

    def on_run_progress(self, message: RunProgress) -> None:
        self.sub_title = f"running checks: {message.count}/{message.total}"
        self._run_progress_bar().update(
            total=message.total, progress=message.count
        )
        if message.result is not None:
            self._apply_live_result(message.result)

    def _apply_live_result(self, result: Result) -> None:
        """Flip ``result``'s own check glyph live, on whichever grid(s)
        show it, the moment it arrives -- rather than waiting for the
        end-of-run authoritative refresh (``on_worker_state_changed``'s
        SUCCESS branch), which only runs once the whole run (every
        source, every check) has finished.

        Recorded into ``self._live_results`` first, keyed by ``check_id``,
        so the Home rollup below always has every one of this run's
        results-so-far for the object, not just the one that just
        arrived. A result with no ``check_id`` (shouldn't happen for a
        real run -- every check gets one -- but nothing here depends on
        it) is ignored rather than corrupting the map with a ``None``
        key.
        """
        if result.check_id is None:
            return
        self._live_results[result.check_id] = result
        self._apply_live_result_to_home(result)

        from dbfresh.tui.screens import ObjectDetailScreen

        if isinstance(self.screen, ObjectDetailScreen):
            self.screen.apply_live_result(result)

    def _apply_live_result_to_home(self, result: Result) -> None:
        """Update the Home grid's object row for ``result``: its
        ``overall`` cell and today's day cell together, both from the same
        worst-or-unknown rollup over this run's results-so-far for that
        object -- the same invariant ``_rollup`` keeps for a completed
        run (an object's ``overall`` and its latest-of-day both use that
        one rule), so the two never read differently mid-run either.

        A no-op when the row isn't currently in ``self._rows_by_key`` --
        hidden by the non-OK filter or a search, or the grid not yet
        populated -- since there is then no cell to update surgically;
        ``DataTable.update_cell`` would raise for a row key the table
        doesn't hold. The end-of-run refresh reconciles a hidden row from
        the store regardless, so it is never left stale, only
        un-animated while hidden.
        """
        row_key = f"{result.source}\x1f{result.object}"
        if row_key not in self._rows_by_key:
            return
        config = self._require_config()
        object_check_ids = {
            check_id(c)
            for c in config.checks
            if c.source == result.source and c.object == result.object
        }
        statuses = [
            live.status
            for cid, live in self._live_results.items()
            if cid in object_check_ids
        ]
        overall = _worst_or_unknown(statuses)
        table = self.query_one(f"#{_GRID_ID}", DataTable)
        table.update_cell(row_key, "overall", _status_cell(overall))

        from dbfresh.report import display_timezone

        tz = display_timezone(config.calendar)
        today = datetime.now(tz).date()
        # No marker: a day cell's trailing marker flags a worse status the
        # object also saw earlier that same day (see _day_marker), which
        # needs today's full history -- only available from the store,
        # not this run's own in-memory results. The end-of-run
        # refresh_dashboard() recomputes the marker from the store; a live
        # update between now and then just shows this run's current state
        # without one.
        table.update_cell(row_key, today.isoformat(), _day_cell(overall, None))

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Pick up a finished run and refresh the dashboard from it.

        A run that gets cancelled (superseded by a later keypress on an
        exclusive worker group) leaves the dashboard and ``last_run``
        untouched rather than raising -- surfaced as a brief notice instead
        of cancelling silently. A run that errors (store locked past its
        busy timeout, disk full, etc.) is caught the same way --
        ``exit_on_error=False`` on the worker keeps the exception from
        tearing down the whole app -- and is instead surfaced as an error
        toast, again leaving the dashboard and ``last_run`` untouched.
        """
        if event.worker.group != _RUN_WORKER_GROUP:
            return
        if event.state == WorkerState.RUNNING:
            self.sub_title = "running checks…"
            # A fresh map per run: a stale result left over from a
            # previous run (or one cancelled mid-flight) must never
            # contribute to this run's live rollup on either grid.
            self._live_results = {}
            bar = self._run_progress_bar()
            bar.display = True
            # The total isn't known yet at this point -- filter_checks
            # hasn't run -- so only progress resets here; the first
            # RunProgress message (on_run_progress) sets the total.
            bar.update(progress=0)
            return
        if event.state == WorkerState.CANCELLED:
            self._run_progress_bar().display = False
            self.notify(
                "run cancelled -- a newer run started",
                title="Run cancelled",
                severity="warning",
            )
            return

        self.sub_title = ""
        self._run_progress_bar().display = False
        if event.state == WorkerState.SUCCESS:
            run = event.worker.result
            assert run is not None
            self.last_run = run
            self.refresh_dashboard()
            self._refresh_topmost_screen()
            message = _run_summary(run)
            needs_review = run.status in (
                Status.WARN,
                Status.FAIL,
                Status.ERROR,
            )
            # Only point at 'p' when the report can actually be opened from
            # here: it is a Home-only action (see _HOME_ONLY_ACTIONS), so a
            # run that finishes with a screen pushed on top of Home -- e.g. a
            # scoped run started from the object-detail screen -- has no
            # reachable report to offer. Reuse check_action so the hint and
            # the binding can never disagree.
            report_reachable = self.check_action("report", ()) is True
            if needs_review and report_reachable:
                message = f"{message} -- press 'p' for the report"
            self.notify(
                message,
                title="Run complete",
                severity=_RUN_TOAST_SEVERITY[run.status],
                timeout=10 if needs_review else None,
            )
        elif event.state == WorkerState.ERROR:
            self.notify(
                f"check run failed: {event.worker.error}",
                title="Run failed",
                severity="error",
                timeout=10,
            )

    def _refresh_topmost_screen(self) -> None:
        """Refresh whichever pushed screen is on top, if it shows run data.

        ``refresh_dashboard`` above only ever touches the Home grid --
        ``App.query_one`` always queries the default screen, never
        whichever screen is actually active (see ``App._get_dom_base``) --
        so a screen pushed on top of Home (``ObjectDetailScreen``,
        ``ReportScreen``) needs its own refresh call to pick up a run that
        completed while it was showing, rather than only updating once the
        user pops back to Home and back in.
        """
        from dbfresh.tui.screens import ObjectDetailScreen, ReportScreen

        if isinstance(self.screen, ObjectDetailScreen):
            self.screen.refresh_grid()
        elif isinstance(self.screen, ReportScreen):
            self.screen.refresh_report(self.last_run)

    def action_configure(self) -> None:
        from dbfresh.tui.configure import ConfigureScreen

        self.push_screen(
            ConfigureScreen(self.config_path, self._require_config()),
            self._on_configure_dismissed,
        )

    def _on_configure_dismissed(self, wrote: bool | None) -> None:
        if not wrote:
            return
        try:
            self._reload_config()
        except Exception as exc:
            self.notify(
                f"config reload failed after write: {exc}",
                title="Reload failed",
                severity="error",
                timeout=10,
            )
            return
        self.refresh_dashboard()

        # Connect the two steps a first-time user would otherwise have to
        # discover separately: a just-written check renders as unknown
        # ("never observed") until something runs it, so run immediately
        # rather than leaving that connection for the user to find via 'r'.
        self.notify("running the checks you just configured…")
        self.action_run_checks()

    def action_report(self) -> None:
        from dbfresh.report import display_timezone
        from dbfresh.tui.screens import ReportScreen

        tz = display_timezone(self._require_config().calendar)
        self.push_screen(
            ReportScreen(
                self.last_run,
                self._require_store(),
                tz=tz,
                checks=self._require_config().checks,
            )
        )

    def action_store(self) -> None:
        from dbfresh.tui.screens import StoreScreen

        config = self._require_config()
        retain_days = (config.store or StoreConfig()).retain_days
        self.push_screen(StoreScreen(self._require_store(), retain_days))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Selecting an object row opens its checks (ObjectDetailScreen).

        Guarded to the Home grid specifically (``event.data_table.id``):
        ObjectDetailScreen's own grid also emits ``RowSelected``, but stops
        that event itself before it would otherwise bubble up here.
        """
        if event.data_table.id != _GRID_ID or event.row_key.value is None:
            return
        row = self._rows_by_key.get(event.row_key.value)
        if row is None or row.source is None or row.object is None:
            return
        from dbfresh.report import display_timezone
        from dbfresh.tui.screens import ObjectDetailScreen

        tz = display_timezone(self._require_config().calendar)
        self.push_screen(
            ObjectDetailScreen(
                self._require_store(),
                self._require_config(),
                self.config_path,
                row.source,
                row.object,
                tz=tz,
            ),
            self._on_object_detail_dismissed,
        )

    def reload_config_from_disk(self) -> Config | None:
        """Re-read config_path into self.config after a write this session
        made, and bring the dashboard back in step. Returns the reloaded
        Config, or None (after surfacing a toast) when the reload failed,
        leaving the prior config in place. Shared by the object-detail
        dismiss path and by an inline save there, so an immediate scoped run
        reads the just-saved values rather than the stale in-memory config."""
        try:
            self._reload_config()
        except Exception as exc:
            self.notify(
                f"config reload failed after write: {exc}",
                title="Reload failed",
                severity="error",
                timeout=10,
            )
            return None
        self.refresh_dashboard()
        return self._require_config()

    def _on_object_detail_dismissed(self, changed: bool | None) -> None:
        """Mirrors :meth:`_on_configure_dismissed`: an edit or delete made
        from the drill-in already wrote straight to disk (unlike Configure's
        own Accept, there's no staged bundle to write here), so all that's
        left is bringing Home's own config and dashboard back in step with
        it. No auto-run afterward -- unlike a newly configured check, an
        edited threshold or a deleted check has no "never observed" status
        to resolve by running immediately.
        """
        if not changed:
            return
        self.reload_config_from_disk()

    def on_unmount(self) -> None:
        if self.store is not None:
            self.store.close()
