"""Report, History, and object-detail screens pushed from the Home grid."""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from dbfresh.checks import Check, check_id
from dbfresh.config import Config
from dbfresh.models import RunResult, Status
from dbfresh.report import reconstruct_run, render_digest, render_history
from dbfresh.store import Store
from dbfresh.tui.dashboard import (
    GridRow,
    check_label,
    check_rows,
    populate_grid,
    status_glyph,
    status_legend,
    status_style,
)

_NO_RUN_MESSAGE = "no runs recorded yet -- press 'r' on the dashboard to run checks"

# dbfresh.tui.app.tcss's $subtext0 -- Rich Text styling (used for the
# reconstruction note below) can't reference a Textual CSS variable, so the
# hex is duplicated here to match the same muted-metadata convention the
# Home dashboard's last-run line uses (dbfresh.tui.dashboard.last_run_line).
_SUBTEXT0 = "#a5adcb"

# Shown above a Report reconstructed from the store rather than from an
# in-session run, so a restart's report doesn't silently imply the fuller
# detail (violating-row samples, schema diff) a live run's report can show
# but a reconstruction never has -- see report.reconstruct_run.
_RECONSTRUCTED_NOTE = (
    "(reconstructed from stored observations -- sample rows and schema diff "
    "detail are not available)"
)

# render_history's own fixed-width columns (see dbfresh.report.render_history:
# f"{observed:<28} {row['status']:<8} {display:<16} {trend}") -- used below to
# locate the status field within each already-rendered row line rather than
# recomputing it, so the CLI's formatting stays the single source of truth.
_HISTORY_OBSERVED_WIDTH = 28
_HISTORY_STATUS_WIDTH = 8


def _colorized_digest(run: RunResult, tz: tzinfo | None) -> Text:
    """:func:`render_digest`'s plain text, recolored by status severity for
    the Report screen.

    ``render_digest`` prefixes every non-OK/SKIPPED block with the same
    literal glyph ("✗ "), so WARN, FAIL, and ERROR read identically in the
    plain-text digest the CLI prints -- that text stays untouched here.
    This walks the same non-OK/SKIPPED results in the same order
    ``render_digest`` iterates ``run.results`` to recover each block's
    status, then recolors that block's header line with the grid's own
    glyph/style for that status (:func:`~dbfresh.tui.dashboard.status_glyph`,
    :func:`~dbfresh.tui.dashboard.status_style`).

    The walk is defensive: if it ever falls out of step with the digest
    text -- a future change to ``render_digest``'s line format -- the
    affected line falls back to uncolored rather than raising.
    """
    plain = render_digest(run, tz=tz)
    blocks = iter(
        result
        for result in run.results
        if result.status not in (Status.OK, Status.SKIPPED)
    )
    lines: list[Text] = []
    for line in plain.split("\n"):
        if line.startswith("✗ "):
            result = next(blocks, None)
            if result is not None:
                styled = Text(
                    status_glyph(result.status), style=status_style(result.status)
                )
                styled.append(line[1:])
                lines.append(styled)
                continue
        lines.append(Text(line))
    return Text("\n").join(lines)


def _colorized_history(candidate: dict, rows: list[dict], tz: tzinfo | None) -> Text:
    """:func:`render_history`'s plain text, recolored for the History
    screen the same way :func:`_colorized_digest` recolors the Report
    digest -- ``render_history`` itself (also the CLI's ``dbfresh
    history`` output) is left untouched; only this presentation layer
    reads and restyles its text.

    Two changes: each row's bare status word becomes a glyph+color pair
    via :func:`~dbfresh.tui.dashboard.status_glyph` /
    :func:`~dbfresh.tui.dashboard.status_style` -- the same encoding the
    grid and the Report digest already use, so History is no longer the
    one surface where a status escapes it -- and the heading drops the
    trailing ``(check_id)`` hash, which is noise on a screen already
    reached by selecting that exact check.

    ``render_history`` appends exactly one line per row, in ``rows``
    order, after its header lines, so the last ``len(rows)`` lines line up
    with ``rows`` positionally without needing to locate the header by
    content.
    """
    plain = render_history(candidate, rows, tz=tz)
    lines = plain.split("\n")
    lines[0] = lines[0].removesuffix(f" ({candidate['check_id']})")

    if rows:
        header_lines, data_lines = lines[: -len(rows)], lines[-len(rows) :]
    else:
        header_lines, data_lines = lines, []

    status_start = _HISTORY_OBSERVED_WIDTH + 1
    status_end = status_start + _HISTORY_STATUS_WIDTH
    # "glyph status" runs up to two chars wider than the bare status word --
    # "– SKIPPED" is 9, one past render_history's 8-char status field -- so
    # give the styled cells one extra column and widen the column header's
    # status slot to match, keeping the value column aligned on every row,
    # SKIPPED included.
    field = _HISTORY_STATUS_WIDTH + 1
    styled = [Text(line) for line in header_lines]
    if rows:
        header = header_lines[-1]
        styled[-1] = Text(
            header[:status_start]
            + header[status_start:status_end].ljust(field)
            + header[status_end:]
        )
    for row, line in zip(rows, data_lines, strict=True):
        status = Status(row["status"])
        entry = Text(line[:status_start])
        label = f"{status_glyph(status)} {status}".ljust(field)
        entry.append(label, style=status_style(status))
        entry.append(line[status_end:])
        styled.append(entry)
    return Text("\n").join(styled)


class ReportScreen(Screen):
    """The most recent run's digest, via :func:`render_digest`.

    Prefers the in-session ``run`` (fuller detail: violating-row samples,
    schema diff) when one exists. Once the app has run at least one check
    this session, ``run`` is always set here -- :meth:`refresh_report`
    keeps it current. Absent that (a fresh session -- app just launched, or
    "p" pressed before "r"), falls back to reconstructing the most recent
    *completed* run from ``store`` (:func:`~dbfresh.report.reconstruct_run`)
    so a restart still shows the last real result rather than nothing; that
    reconstruction is missing samples/diff (never persisted), so its digest
    is prefixed with a note saying so. Only when the store has no completed
    run either -- a genuinely fresh install -- is :data:`_NO_RUN_MESSAGE`
    shown.
    """

    BINDINGS = [Binding("escape", "dismiss_screen", "Back")]

    def __init__(
        self,
        run: RunResult | None,
        store: Store | None = None,
        tz: tzinfo | None = None,
    ) -> None:
        super().__init__()
        self._run = run
        self._store = store
        self._tz = tz

    def compose(self) -> ComposeResult:
        yield Header()
        body = Static(self._render_body(), id="report-text", markup=False)
        yield VerticalScroll(body)
        yield Footer()

    def _render_body(self) -> str | Text:
        if self._run is not None:
            return _colorized_digest(self._run, tz=self._tz)
        if self._store is not None:
            stored_run = self._store.latest_run()
            if stored_run is not None:
                observations = self._store.observations_for_run(stored_run["run_id"])
                reconstructed = reconstruct_run(stored_run, observations)
                digest = _colorized_digest(reconstructed, tz=self._tz)
                note = Text(_RECONSTRUCTED_NOTE, style=_SUBTEXT0)
                return Text.assemble(note, "\n\n", digest)
        return _NO_RUN_MESSAGE

    def refresh_report(self, run: RunResult | None) -> None:
        """Re-render from ``run`` -- the app's Run action calls this on a
        completed run when this screen is the one currently on top, since
        ``compose`` above only ever renders once, at push time, off of
        whatever ``run`` its constructor was given."""
        self._run = run
        self.query_one("#report-text", Static).update(self._render_body())

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
            "metric": self._check.metric,
        }
        rows = self._store.history(cid)
        text = _colorized_history(candidate, rows, tz=self._tz)
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
        yield DataTable(
            id=_DETAIL_GRID_ID,
            cursor_type="row",
            zebra_stripes=True,
            cell_padding=2,
            # See DbfreshApp.compose's dashboard-grid DataTable -- same
            # reason: keep each cell's own status color on the cursor row.
            cursor_foreground_priority="renderable",
        )
        yield Static(status_legend(), id="status-legend")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_grid()

    def refresh_grid(self) -> None:
        """(Re)populate this object's check grid from the store's current
        observations -- also called by the app's Run action when this
        screen is the one currently on top of a just-completed run, so its
        statuses update without the user having to pop back to Home and
        back in."""
        table = self.query_one(f"#{_DETAIL_GRID_ID}", DataTable)
        today = datetime.now(self._tz or UTC).date()
        rows = check_rows(
            self._source, self._object, self._config, self._store, today, self._tz
        )
        populate_grid(table, rows, today, label_header="check")
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
