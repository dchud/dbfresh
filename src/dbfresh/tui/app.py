"""The dbfresh ui Textual application.

A presentation layer only: the Home dashboard, Run action, and Configure /
Report / History destinations all read and write through the same
config/store/engine/configurator modules the CLI uses. No check semantics
live here.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Header, Static
from textual.worker import Worker, WorkerState

from dbfresh.config import Config, load_config
from dbfresh.models import RunResult
from dbfresh.store import Store, resolve_store_path
from dbfresh.tui.dashboard import GridRow, object_rows, populate_grid, status_legend

_GRID_ID = "dashboard-grid"
_RUN_WORKER_GROUP = "run-checks"


class DbfreshApp(App):
    """Status dashboard over ``config_path``'s checks and ``store_path``."""

    TITLE = "dbfresh"

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
        self.config_path = Path(config_path)
        self._store_path_override = store_path
        self.config: Config | None = initial_config
        self.store: Store | None = None
        self.last_run: RunResult | None = None
        self._rows_by_key: dict[str, GridRow] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id=_GRID_ID, cursor_type="row")
        yield Static(status_legend(), id="status-legend")
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
        """Rebuild the dashboard grid from the current config and store."""
        from dbfresh.report import display_timezone

        table = self.query_one(f"#{_GRID_ID}", DataTable)
        config = self._require_config()
        tz = display_timezone(config.calendar)
        today = datetime.now(tz).date()
        rows = object_rows(config, self._require_store(), today, tz)
        populate_grid(table, rows, today, label_header="object")
        self._rows_by_key = {row.key: row for row in rows}

    def action_run_checks(self) -> None:
        """Start a check run in a worker thread; the UI stays responsive."""
        self._run_checks_worker()

    @work(thread=True, exclusive=True, group=_RUN_WORKER_GROUP, exit_on_error=False)
    def _run_checks_worker(self) -> RunResult:
        from dbfresh.runner import run_and_persist

        return run_and_persist(self._require_config(), self.store)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Pick up a finished run and refresh the dashboard from it.

        A run that gets cancelled (superseded by a later keypress on an
        exclusive worker group) leaves the dashboard and ``last_run``
        untouched rather than raising. A run that errors (store locked
        past its busy timeout, disk full, etc.) is caught the same way --
        ``exit_on_error=False`` on the worker keeps the exception from
        tearing down the whole app -- and is instead surfaced as an error
        toast, again leaving the dashboard and ``last_run`` untouched.
        """
        if event.worker.group != _RUN_WORKER_GROUP:
            return
        if event.state == WorkerState.SUCCESS:
            self.last_run = event.worker.result
            self.refresh_dashboard()
        elif event.state == WorkerState.ERROR:
            self.notify(
                f"check run failed: {event.worker.error}",
                title="Run failed",
                severity="error",
                timeout=10,
            )

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
