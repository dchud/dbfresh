"""Report and History screens pushed from the Home dashboard."""

from __future__ import annotations

from datetime import tzinfo

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

from dbfresh.checks import Check, check_id
from dbfresh.models import RunResult
from dbfresh.report import render_digest, render_history
from dbfresh.store import Store
from dbfresh.tui.dashboard import check_label

_NO_RUN_MESSAGE = (
    "no run recorded in this session yet -- press 'r' on the dashboard to run checks"
)


class ReportScreen(Screen):
    """The most recent in-session run's digest, via :func:`render_digest`.

    ``run`` is ``None`` until the user has triggered at least one in-app run
    (the store's flattened observations don't retain enough to reconstruct
    a full digest -- samples, diffs, and error text aren't persisted).
    """

    BINDINGS = [Binding("escape", "dismiss_screen", "Back")]

    def __init__(self, run: RunResult | None, tz: tzinfo | None = None) -> None:
        super().__init__()
        self._run = run
        self._tz = tz

    def compose(self) -> ComposeResult:
        text = (
            render_digest(self._run, tz=self._tz)
            if self._run is not None
            else _NO_RUN_MESSAGE
        )
        yield Header()
        yield VerticalScroll(Static(text, id="report-text", markup=False))
        yield Footer()

    def action_dismiss_screen(self) -> None:
        self.app.pop_screen()


class HistoryScreen(Screen):
    """A selected check's recent values, statuses, and trend.

    The interactive form of ``dbfresh history``, over the same
    :meth:`~dbfresh.store.Store.history` and :func:`render_history` the CLI
    uses.
    """

    BINDINGS = [Binding("escape", "dismiss_screen", "Back")]

    def __init__(self, store: Store, check: Check, tz: tzinfo | None = None) -> None:
        super().__init__()
        self._store = store
        self._check = check
        self._tz = tz

    def compose(self) -> ComposeResult:
        cid = check_id(self._check)
        candidate = {
            "check_id": cid,
            "source": self._check.source,
            "object": self._check.object,
            "label": check_label(self._check),
        }
        rows = self._store.history(cid)
        text = render_history(candidate, rows, tz=self._tz)
        yield Header()
        yield VerticalScroll(Static(text, id="history-text", markup=False))
        yield Footer()

    def action_dismiss_screen(self) -> None:
        self.app.pop_screen()
