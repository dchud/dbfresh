"""Report, History, and object-detail screens pushed from the Home grid."""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from dbfresh.checks import Check, check_id
from dbfresh.config import Config
from dbfresh.models import RunResult
from dbfresh.report import render_digest, render_history
from dbfresh.store import Store
from dbfresh.tui.dashboard import GridRow, check_label, check_rows, populate_grid

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
    uses. Reached either from :class:`ObjectDetailScreen` (the Home grid's
    drill-in) or directly wherever a caller already has a specific
    :class:`~dbfresh.checks.Check` in hand.
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


_DETAIL_GRID_ID = "object-detail-grid"


class ObjectDetailScreen(Screen):
    """One object's checks as a status grid -- the Home grid's drill-in.

    The Home grid's rows are one per source.object (rolled up across all of
    an object's checks); this screen shows that object's individual checks
    at the same [overall, trailing-days] shape, via the same
    :func:`~dbfresh.tui.dashboard.populate_grid` renderer just scoped one
    level down (:func:`~dbfresh.tui.dashboard.check_rows` instead of
    ``object_rows``). Selecting a row here opens :class:`HistoryScreen` for
    that specific check -- the same destination the old nested tree's leaf
    selection reached directly; this screen is the one extra hop the
    flatter, object-level Home grid now needs to reach individual check
    detail.
    """

    BINDINGS = [Binding("escape", "dismiss_screen", "Back")]

    def __init__(
        self,
        store: Store,
        config: Config,
        source: str,
        object_: str,
        tz: tzinfo | None = None,
    ) -> None:
        super().__init__()
        self._store = store
        self._config = config
        self._source = source
        self._object = object_
        self._tz = tz
        self._rows_by_key: dict[str, GridRow] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(f"{self._source}.{self._object}", id="object-detail-heading")
        yield DataTable(id=_DETAIL_GRID_ID, cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(f"#{_DETAIL_GRID_ID}", DataTable)
        today = datetime.now(self._tz or UTC).date()
        rows = check_rows(
            self._source, self._object, self._config, self._store, today, self._tz
        )
        populate_grid(table, rows, today)
        self._rows_by_key = {row.key: row for row in rows}

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        if event.row_key.value is None:
            return
        row = self._rows_by_key.get(event.row_key.value)
        if row is None or row.check is None:
            return
        self.app.push_screen(HistoryScreen(self._store, row.check, tz=self._tz))

    def action_dismiss_screen(self) -> None:
        self.app.pop_screen()
