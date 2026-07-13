"""The dbfresh ui Textual application.

A presentation layer only: the Home dashboard, Run action, and Configure /
Report / History destinations all read and write through the same
config/store/engine/configurator modules the CLI uses. No check semantics
live here.
"""

from __future__ import annotations

import os
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, Tree
from textual.worker import Worker, WorkerState

from dbfresh.config import Config, load_config
from dbfresh.engine import RunResult
from dbfresh.store import Store, resolve_store_path
from dbfresh.tui.dashboard import NodeInfo, build_dashboard

_TREE_ID = "dashboard-tree"
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

    def __init__(self, config_path: str | Path, store_path: str | None = None) -> None:
        super().__init__()
        self.config_path = Path(config_path)
        self._store_path_override = store_path
        self.config: Config | None = None
        self.store: Store | None = None
        self.last_run: RunResult | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Tree("dbfresh", id=_TREE_ID)
        yield Footer()

    def on_mount(self) -> None:
        self._reload_config()
        self._open_store()
        self.refresh_dashboard()

    def _reload_config(self) -> None:
        self.config = load_config(self.config_path)

    def _open_store(self) -> None:
        store_path = resolve_store_path(
            config_dir=self.config.config_dir,
            store_config=self.config.store,
            cli_store=self._store_path_override,
            env_store=os.environ.get("DBFRESH_STORE"),
        )
        self.store = Store(store_path)

    def refresh_dashboard(self) -> None:
        """Rebuild the dashboard tree from the current config and store."""
        tree = self.query_one(f"#{_TREE_ID}", Tree)
        build_dashboard(tree, self.config, self.store)

    def action_run_checks(self) -> None:
        """Start a check run in a worker thread; the UI stays responsive."""
        self._run_checks_worker()

    @work(thread=True, exclusive=True, group=_RUN_WORKER_GROUP)
    def _run_checks_worker(self) -> RunResult:
        from dbfresh.runner import run_and_persist

        return run_and_persist(self.config, self.store)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Pick up a finished run and refresh the dashboard from it.

        A run that errors or gets cancelled (superseded by a later
        keypress on an exclusive worker group) leaves the dashboard and
        ``last_run`` untouched rather than raising.
        """
        if event.worker.group != _RUN_WORKER_GROUP:
            return
        if event.state == WorkerState.SUCCESS:
            self.last_run = event.worker.result
            self.refresh_dashboard()

    def action_configure(self) -> None:
        from dbfresh.tui.configure import ConfigureScreen

        self.push_screen(
            ConfigureScreen(self.config_path, self.config),
            self._on_configure_dismissed,
        )

    def _on_configure_dismissed(self, wrote: bool | None) -> None:
        if wrote:
            self._reload_config()
            self.refresh_dashboard()

    def action_report(self) -> None:
        from dbfresh.report import display_timezone
        from dbfresh.tui.screens import ReportScreen

        tz = display_timezone(self.config.calendar)
        self.push_screen(ReportScreen(self.last_run, tz=tz))

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        """Selecting a check leaf opens its History drill-down."""
        info = event.node.data
        if isinstance(info, NodeInfo) and info.kind == "check" and info.check:
            from dbfresh.report import display_timezone
            from dbfresh.tui.screens import HistoryScreen

            tz = display_timezone(self.config.calendar)
            self.push_screen(HistoryScreen(self.store, info.check, tz=tz))

    def on_unmount(self) -> None:
        if self.store is not None:
            self.store.close()
