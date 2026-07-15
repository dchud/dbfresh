"""The dbfresh ui Textual application.

A presentation layer only: the Home dashboard, Run action, and Configure /
Report / History destinations all read and write through the same
config/store/engine/configurator modules the CLI uses. No check semantics
live here.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.notifications import SeverityLevel
from textual.widgets import DataTable, Footer, Header, Static
from textual.worker import Worker, WorkerState

from dbfresh.config import Config, load_config
from dbfresh.models import Result, RunResult, Status
from dbfresh.store import Store, resolve_store_path
from dbfresh.tui.dashboard import GridRow, object_rows, populate_grid, status_legend

_GRID_ID = "dashboard-grid"
_RUN_WORKER_GROUP = "run-checks"

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
_EMPTY_STATE_MESSAGE = (
    "no checks configured yet -- press 'c' to configure a source and its checks"
)


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
    """

    def __init__(self, count: int, total: int) -> None:
        self.count = count
        self.total = total
        super().__init__()


class DbfreshApp(App):
    """Status dashboard over ``config_path``'s checks and ``store_path``."""

    TITLE = "dbfresh"
    CSS_PATH = "app.tcss"

    BINDINGS = [
        Binding("r", "run_checks", "Run"),
        Binding("c", "configure", "Configure"),
        Binding("p", "report", "Report"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        config_path: str | Path,
        store_path: str | None = None,
        initial_config: Config | None = None,
    ) -> None:
        """Build the app; ``initial_config``, when given, is used as-is at
        mount time instead of re-parsing ``config_path``.

        ``dbfresh ui`` (``cli._ui_command``) already parses the config once
        to fail cleanly before the Textual session ever starts; passing
        that same :class:`~dbfresh.config.Config` through here avoids
        parsing the same unchanged file a second time. Omit it (the
        default) to have :meth:`on_mount` load it itself -- what every
        test that constructs ``DbfreshApp`` directly relies on.
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

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(
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
        yield Static(_EMPTY_STATE_MESSAGE, id="empty-state")
        yield Footer()

    def on_mount(self) -> None:
        if self.config is None:
            self._reload_config()
        self._open_store()
        self.refresh_dashboard()

    def _reload_config(self) -> None:
        self.config = load_config(self.config_path)

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
        """Rebuild the dashboard grid from the current config and store.

        A config with no checks (or no sources to hang any on) has no rows
        at all -- rather than showing the grid's bare header row, swap in
        an empty-state hint pointing at Configure.
        """
        from dbfresh.report import display_timezone

        table = self.query_one(f"#{_GRID_ID}", DataTable)
        config = self._require_config()
        tz = display_timezone(config.calendar)
        today = datetime.now(tz).date()
        rows = object_rows(config, self._require_store(), today, tz)
        populate_grid(table, rows, today, label_header="object")
        self._rows_by_key = {row.key: row for row in rows}

        empty = not rows
        table.display = not empty
        self.query_one("#status-legend", Static).display = not empty
        self.query_one("#empty-state", Static).display = empty

    def action_run_checks(self) -> None:
        """Start a check run in a worker thread; the UI stays responsive."""
        self._run_checks_worker()

    @work(thread=True, exclusive=True, group=_RUN_WORKER_GROUP, exit_on_error=False)
    def _run_checks_worker(self) -> RunResult:
        """Run every check, posting a :class:`RunProgress` message per
        completed one along the way.

        ``on_result`` (see ``dbfresh.runner.run_and_persist`` /
        ``dbfresh.engine.run_checks``) fires from whichever per-source
        worker thread just finished a check, potentially several at once
        -- ``count`` is only ever mutated under ``lock``, and every update
        reaches the UI via ``post_message`` rather than by touching a
        widget or reactive attribute from these threads directly.
        """
        from dbfresh.runner import run_and_persist

        config = self._require_config()
        total = len(config.checks)
        lock = threading.Lock()
        count = 0

        def on_result(_result: Result) -> None:
            nonlocal count
            with lock:
                count += 1
                current = count
            self.post_message(RunProgress(current, total))

        return run_and_persist(config, self.store, on_result=on_result)

    def on_run_progress(self, message: RunProgress) -> None:
        self.sub_title = f"running checks: {message.count}/{message.total}"

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
            return
        if event.state == WorkerState.CANCELLED:
            self.notify(
                "run cancelled -- a newer run started",
                title="Run cancelled",
                severity="warning",
            )
            return

        self.sub_title = ""
        if event.state == WorkerState.SUCCESS:
            run = event.worker.result
            assert run is not None
            self.last_run = run
            self.refresh_dashboard()
            self._refresh_topmost_screen()
            self.notify(
                _run_summary(run),
                title="Run complete",
                severity=_RUN_TOAST_SEVERITY[run.status],
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
        self.push_screen(ReportScreen(self.last_run, tz=tz))

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
                row.source,
                row.object,
                tz=tz,
            )
        )

    def on_unmount(self) -> None:
        if self.store is not None:
            self.store.close()
