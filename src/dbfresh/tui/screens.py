"""Report, History, and object-detail screens pushed from the Home grid."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, tzinfo
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import ActiveBinding, Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.timer import Timer
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Label,
    OptionList,
    Static,
)
from textual.widgets.option_list import Option
from textual.worker import Worker, WorkerState

from dbfresh.checks import Check, check_id
from dbfresh.config import Config
from dbfresh.models import Result, RunResult, Status
from dbfresh.report import (
    _format_freshness_observed,
    _format_observed,
    reconstruct_run,
    render_digest,
    render_history,
)
from dbfresh.store import Store, format_bytes
from dbfresh.tui.dashboard import (
    DrillDownTable,
    GridRow,
    _status_cell,
    cancel_flashes,
    check_label,
    check_line_renderable,
    check_rows,
    flash_cell,
    populate_grid,
    status_glyph,
    status_legend,
    status_style,
)

# dbfresh.tui.app is deliberately absent from this block. app.py imports
# this module's screen classes to push them, and the few methods here that
# need the concrete DbfreshApp (to run this object's checks, to read the
# run's results-so-far) import it inside the function body instead. Having
# both directions at module level is not a style question but a hard
# failure: whichever module is imported first hits "cannot import name ...
# from partially initialized module". app.py composes screens, so that is
# the direction that keeps its imports at the top, and this back-reference
# is the one that defers.

_NO_RUN_MESSAGE = (
    "no runs recorded yet -- press 'r' on the dashboard to run checks"
)

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
# f"{observed:<28} {row['status']:<8} {display:<...} {expected:<...}") -- used
# below to locate the status field within each already-rendered row line
# rather than recomputing it, so the CLI's formatting stays the single
# source of truth. Only observed_at and status are needed here -- both
# precede the value/expected columns, whose own widths (and any growth,
# e.g. a freshness row's wider reconstructed-timestamp value) don't shift
# where those two fields start or end.
_HISTORY_OBSERVED_WIDTH = 28
_HISTORY_STATUS_WIDTH = 8


def _digest_segments(
    run: RunResult, tz: tzinfo | None
) -> tuple[Text, list[tuple[Result, Text]]]:
    """The colorized digest split for the Report screen: the 2-line header,
    and one ``(result, block)`` pair per non-OK/SKIPPED check.

    Same walk :func:`_colorized_digest` builds on top of this for -- see
    its own docstring for the recoloring rule and the defensive fallback --
    just grouped into per-check blocks instead of one joined ``Text``, so
    the Report screen can offer each block as its own selectable
    ``OptionList`` option rather than one static paragraph.

    ``render_digest`` always emits a blank line right before every ``✗
    ``-prefixed block header and never inside a block's own body lines
    (see its own loop), so grouping on blank lines recovers exactly the
    same blocks :func:`_colorized_digest` would recolor line-by-line. A
    ``✗ `` line the ``blocks`` walk can't match to a result (the same
    out-of-step case ``_colorized_digest`` guards) has nothing to pair it
    with here and is dropped from the segment list -- unreachable today,
    same as there.
    """
    plain = render_digest(run, tz=tz)
    blocks = iter(
        result
        for result in run.results
        if result.status not in (Status.OK, Status.SKIPPED)
    )
    plain_lines = plain.split("\n")
    header = Text("\n").join(
        [Text(plain_lines[0], style="bold"), Text(plain_lines[1])]
    )

    segments: list[tuple[Result, Text]] = []
    current_result: Result | None = None
    current_lines: list[Text] = []

    def _flush() -> None:
        if current_result is not None and current_lines:
            segments.append((current_result, Text("\n").join(current_lines)))

    for line in plain_lines[2:]:
        if line == "":
            _flush()
            current_result = None
            current_lines = []
            continue
        if line.startswith("✗ ") and not current_lines:
            result = next(blocks, None)
            current_result = result
            if result is not None:
                styled = Text(
                    status_glyph(result.status),
                    style=status_style(result.status),
                )
                styled.append(line[1:])
                current_lines.append(styled)
            else:
                current_lines.append(Text(line))
            continue
        current_lines.append(Text(line))
    _flush()
    return header, segments


def _colorized_digest(run: RunResult, tz: tzinfo | None) -> Text:
    """:func:`render_digest`'s plain text, recolored by status severity for
    the Report screen.

    ``render_digest`` prefixes every non-OK/SKIPPED block with the same
    literal glyph ("✗ "), so WARN, FAIL, and ERROR read identically in the
    plain-text digest the CLI prints -- that text stays untouched here.
    Built on top of :func:`_digest_segments` -- the same header-plus-blocks
    split the Report screen's selectable ``OptionList`` uses -- rejoined
    into one ``Text`` here for the non-interactive (reconstructed-run,
    no-run) rendering paths that still show the whole digest as one block.

    ``render_digest``'s own first line (the "DATA CHECK REPORT — ..."
    header, shared verbatim with the CLI's own digest) is bolded here on
    the TUI side only -- the plain-text CLI output itself is untouched.
    """
    header, segments = _digest_segments(run, tz=tz)
    parts: list[Text] = [header]
    for _result, block in segments:
        parts.append(Text(""))
        parts.append(block)
    return Text("\n").join(parts)


def _colorized_history(
    candidate: dict, rows: list[dict], tz: tzinfo | None
) -> Text:
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

    An in-session run's non-OK/SKIPPED blocks are individually selectable
    (see :meth:`_report_widgets`, :meth:`on_option_list_option_selected`):
    picking one opens that check's :class:`HistoryScreen`. The
    reconstructed and no-run digests stay plain text -- their checks are
    not guaranteed to still exist in the current config.
    """

    TITLE = "Report"

    BINDINGS = [Binding("escape", "dismiss_screen", "Back")]

    # The enclosing VerticalScroll is itself focusable by default (so it
    # can be scrolled from the keyboard) and precedes the OptionList in
    # DOM order -- left to Textual's own default auto-focus, that
    # container would win focus instead of the OptionList, and a plain
    # "p" then Enter would do nothing. A no-op when there's no
    # "#report-options" to find (the reconstructed/no-run fallbacks),
    # same as no auto-focus at all -- mirrors ObjectDetailScreen's own
    # override of the same default for the same reason.
    AUTO_FOCUS = "#report-options"

    def __init__(
        self,
        run: RunResult | None,
        store: Store | None = None,
        tz: tzinfo | None = None,
        checks: list[Check] | None = None,
    ) -> None:
        super().__init__()
        self._run = run
        self._store = store
        self._tz = tz
        self._checks_by_id = {check_id(c): c for c in (checks or [])}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Report", classes="screen-heading")
        yield VerticalScroll(*self._report_widgets())
        yield Footer()

    def _report_widgets(self) -> list[Static | OptionList]:
        """The Report's scrollable body.

        An in-session run (the only case where every block's
        :class:`~dbfresh.models.Result` still has a live
        :class:`~dbfresh.checks.Check` behind it, via
        ``self._checks_by_id``) renders the colorized 2-line header as
        its own ``Static`` followed by a selectable ``OptionList`` -- one
        option per non-OK/SKIPPED block, via :func:`_digest_segments` --
        so picking one can jump straight to that check's History. An
        all-OK in-session run has no segments, so no ``OptionList`` at
        all -- just the header. The reconstructed-from-store and no-run
        fallbacks stay exactly as before: the single plain-text ``Static``
        :meth:`_render_body` has always rendered, unselectable -- a
        reconstruction's checks aren't guaranteed to still be in the
        current config (see :meth:`on_option_list_option_selected`).
        """
        if self._run is None:
            return [
                Static(self._render_body(), id="report-text", markup=False)
            ]
        header, segments = _digest_segments(self._run, tz=self._tz)
        widgets: list[Static | OptionList] = [
            Static(header, id="report-text", markup=False)
        ]
        if segments:
            widgets.append(
                OptionList(
                    *(
                        Option(block, id=result.check_id)
                        for result, block in segments
                    ),
                    id="report-options",
                )
            )
        return widgets

    def _render_body(self) -> str | Text:
        if self._run is not None:
            return _colorized_digest(self._run, tz=self._tz)
        if self._store is not None:
            stored_run = self._store.latest_run()
            if stored_run is not None:
                observations = self._store.observations_for_run(
                    stored_run["run_id"]
                )
                reconstructed = reconstruct_run(stored_run, observations)
                digest = _colorized_digest(reconstructed, tz=self._tz)
                note = Text(_RECONSTRUCTED_NOTE, style=_SUBTEXT0)
                return Text.assemble(note, "\n\n", digest)
        return _NO_RUN_MESSAGE

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        """Enter (or a click) on a selectable report block: jump to that
        check's :class:`HistoryScreen`, the same destination
        :meth:`ObjectDetailScreen.on_data_table_row_selected` reaches from
        a grid row.

        The option's id is the block's ``check_id`` (see
        :meth:`_report_widgets`), resolved against ``self._checks_by_id``
        (the current config's checks, keyed by :func:`~dbfresh.checks.check_id`
        -- passed in at push time) rather than trusted as-is, since a check
        could be removed from config between when a run produced this
        result and now. No match notifies instead of crashing.
        """
        cid = event.option.id
        check = self._checks_by_id.get(cid) if cid is not None else None
        if check is None:
            self.notify(
                "no history: check is not in the current config",
                severity="warning",
            )
            return
        assert self._store is not None  # every real caller supplies one
        self.app.push_screen(HistoryScreen(self._store, check, tz=self._tz))

    def refresh_report(self, run: RunResult | None) -> None:
        """Re-render from ``run`` -- the app's Run action calls this on a
        completed run when this screen is the one currently on top, since
        ``compose`` above only ever renders once, at push time, off of
        whatever ``run`` its constructor was given.

        When ``run`` is set, the header ``Static`` and the ``OptionList``
        options are rebuilt from its segments in place; a screen pushed
        without one yet (the reconstructed/no-run fallback had nothing to
        select) gets a fresh ``OptionList`` mounted the first time a run
        arrives. The no-run case is unchanged: :meth:`_render_body`
        re-renders the same lone ``Static`` it always has.
        """
        self._run = run
        if run is None:
            self.query_one("#report-text", Static).update(self._render_body())
            return
        header, segments = _digest_segments(run, tz=self._tz)
        self.query_one("#report-text", Static).update(header)
        options = [
            Option(block, id=result.check_id) for result, block in segments
        ]
        option_list = self.query_one_optional("#report-options", OptionList)
        if option_list is not None:
            option_list.clear_options()
            if options:
                option_list.add_options(options)
            option_list.display = bool(options)
        elif options:
            self.query_one(VerticalScroll).mount(
                OptionList(*options, id="report-options")
            )

    def action_dismiss_screen(self) -> None:
        self.app.pop_screen()


class HistoryScreen(Screen):
    """A selected check's recent values and statuses.

    The interactive form of ``dbfresh history``, over the same
    :meth:`~dbfresh.store.Store.history` and :func:`render_history` the CLI
    uses. Reached either from :class:`ObjectDetailScreen` (the Home grid's
    drill-in) or directly wherever a caller already has a specific
    :class:`~dbfresh.checks.Check` in hand.
    """

    TITLE = "History"

    BINDINGS = [Binding("escape", "dismiss_screen", "Back")]

    def __init__(
        self, store: Store, check: Check, tz: tzinfo | None = None
    ) -> None:
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
        yield Static("History", classes="screen-heading")
        yield VerticalScroll(Static(text, id="history-text", markup=False))
        yield Footer()

    def action_dismiss_screen(self) -> None:
        self.app.pop_screen()


_DETAIL_GRID_ID = "object-detail-grid"
_RUN_OBJECT_BUTTON_ID = "detail-run-object-btn"


def _check_detail_text(check: Check, obs: dict, tz: tzinfo | None) -> Text:
    """The text for ``#check-detail-line`` -- the currently-highlighted
    check's WARN/FAIL/ERROR detail on :class:`ObjectDetailScreen`, so why a
    check is in that state is visible without the extra hop into
    :class:`HistoryScreen`.

    The leading glyph reuses the grid's own
    :func:`~dbfresh.tui.dashboard.status_glyph`/``status_style`` -- the same
    encoding the row itself renders -- but the reason text after it carries
    no style of its own, so it falls back to this screen's own muted
    ``#check-detail-line`` CSS color (app.tcss): status colors stay reserved
    for the glyph, never a bespoke "error" color.

    ERROR shows the persisted error message; WARN/FAIL show what the check
    expected against what it actually observed, reusing
    :func:`~dbfresh.report._format_observed`/``_format_freshness_observed``
    -- the same value formatting the digest already uses (see
    ``render_digest``) -- rather than reformatting the stored value here.
    ``obs["observed_at"]`` stands in for a live run's ``reference`` (the
    "now" that produced a freshness lag), the same substitution
    ``render_history`` makes per row. A long error is whitespace-collapsed
    to one line, the same way ``render_history`` collapses its own error
    column.
    """
    status = Status(obs["status"])
    text = Text()
    text.append(status_glyph(status), style=status_style(status))
    text.append(" ")
    if status == Status.ERROR:
        error = " ".join(str(obs["error"] or "").split())
        text.append(f"error: {error}")
        return text
    value = obs["value"] if obs["value"] is not None else obs["value_text"]
    if check.metric == "freshness" and isinstance(value, (int, float)):
        reference = datetime.fromisoformat(obs["observed_at"])
        observed = _format_freshness_observed(value, reference, tz)
    else:
        observed = _format_observed(check.metric, value)
    text.append(f"expected {obs['expected']} · observed {observed}")
    return text


class ObjectDetailScreen(Screen[None]):
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

    Below the grid, a "Checks" panel lists this object's checks again, each
    with its expectation, read-only -- config is a file the user edits by
    hand, never something this screen writes, so the panel names the config
    path and each check's identity (see
    :func:`~dbfresh.tui.dashboard.check_expectation_line`) rather than
    offering a form to change either.
    """

    TITLE = "Object detail"

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Back"),
        Binding("O", "run_object", "Run these checks"),
    ]

    # Textual's default auto-focus (App.AUTO_FOCUS = "*") lands on the
    # first focusable widget in DOM order -- since the "Run these checks"
    # button above the grid (see compose()) is focusable too, without this
    # it would steal initial focus from the grid instead, breaking Enter's
    # row-drill-in as the screen's own opening behavior.
    AUTO_FOCUS = f"#{_DETAIL_GRID_ID}"

    def _run_object_label(self) -> str:
        """The run affordance's label, singular when this object has exactly
        one check ("Run this check") and plural otherwise ("Run these
        checks"). Used for both the button and the footer binding."""
        count = len(self._object_checks())
        return "Run this check" if count == 1 else "Run these checks"

    @property
    def active_bindings(self) -> dict[str, ActiveBinding]:
        """Reflect the object's check count in the footer's run-affordance
        label the same way the button does -- the class binding's static
        "Run these checks" is shown as the singular form for a lone check.
        Only the description changes; the key and action stay intact."""
        label = self._run_object_label()
        return {
            key: (
                active._replace(
                    binding=replace(active.binding, description=label)
                )
                if active.binding.action == "run_object"
                else active
            )
            for key, active in super().active_bindings.items()
        }

    def __init__(
        self,
        store: Store,
        config: Config,
        config_path: str | Path,
        source: str,
        object_: str,
        tz: tzinfo | None = None,
    ) -> None:
        super().__init__()
        self._store = store
        self._config = config
        self._config_path = Path(config_path)
        self._source = source
        self._object = object_
        self._tz = tz
        self._rows_by_key: dict[str, GridRow] = {}
        # Pending flash_cell clear timers for this screen's grid, keyed by
        # (row_key, column_key) -- see flash_cell's own docstring for why
        # a re-flash of the same cell must cancel its predecessor's timer
        # here rather than let it fire later.
        self._cell_flash_timers: dict[tuple[str, str], Timer] = {}

    def _object_checks(self) -> list[Check]:
        """This object's checks, in config order -- shared by the run-label
        count and the read-only checks panel below the grid."""
        return [
            c
            for c in self._config.checks
            if c.source == self._source and c.object == self._object
        ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Object detail", classes="screen-heading")
        yield Static(
            f"{self._source}.{self._object}", id="object-detail-heading"
        )
        yield Horizontal(
            Button(self._run_object_label(), id=_RUN_OBJECT_BUTTON_ID)
        )
        yield DrillDownTable(
            id=_DETAIL_GRID_ID,
            cursor_type="row",
            zebra_stripes=True,
            cell_padding=2,
            # See DbfreshApp.compose's dashboard-grid DataTable -- same
            # reason: keep each cell's own status color on the cursor row.
            cursor_foreground_priority="renderable",
        )
        detail_line = Static("", id="check-detail-line")
        detail_line.display = False
        yield detail_line
        yield Static(status_legend(), id="status-legend")
        checks = self._object_checks()
        check_lines = (
            [Static(check_line_renderable(c)) for c in checks]
            if checks
            else [Static("(no checks for this object)")]
        )
        yield VerticalScroll(
            Vertical(
                Static("Checks", classes="section-title"),
                Static(
                    f"Defined in {self._config_path} -- edit that file by "
                    "hand to change or remove a check.",
                    id="detail-checks-note",
                ),
                Vertical(*check_lines, id="detail-checks-list"),
                id="detail-checks-section",
                classes="panel",
            ),
            id="detail-checks-scroll",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_grid()

    def refresh_grid(self) -> None:
        """(Re)populate this object's check grid from the store's current
        observations -- also called by the app's Run action when this
        screen is the one currently on top of a just-completed run, so its
        statuses update without the user having to pop back to Home and
        back in.

        ``populate_grid`` clears the table first, which resets its cursor
        to row 0 -- syncing ``#check-detail-line`` here too keeps it
        showing whatever row 0 now is, rather than leaving it stale on a
        row that a mutation may have just deleted or replaced.
        """
        table = self.query_one(f"#{_DETAIL_GRID_ID}", DataTable)
        today = datetime.now(self._tz or UTC).date()
        # A pending flash_cell restore() from a live update just before
        # this repaint would otherwise fire afterward and overwrite a cell
        # populate_grid below just freshly painted -- see cancel_flashes.
        cancel_flashes(self._cell_flash_timers)
        rows = check_rows(
            self._source,
            self._object,
            self._config,
            self._store,
            today,
            self._tz,
        )
        rows = self._seed_live_statuses(rows)
        populate_grid(table, rows, today, label_header="check")
        self._rows_by_key = {row.key: row for row in rows}
        self._sync_check_detail_line(self._current_row_key(table))
        # Keep the button label and the footer's run-affordance label (see
        # active_bindings) in step with the object's current check count.
        self.query_one(
            f"#{_RUN_OBJECT_BUTTON_ID}", Button
        ).label = self._run_object_label()
        self.refresh_bindings()

    def _seed_live_statuses(self, rows: list[GridRow]) -> list[GridRow]:
        """``rows`` with each check's ``overall`` replaced by the status
        the run in flight has already produced for it, where there is one.

        :func:`~dbfresh.tui.dashboard.check_rows` builds every row from the
        store, and a run's observations are written in one batch only once
        the whole run finishes -- so mid-run the store holds nothing about
        it. :meth:`apply_live_result` covers results that arrive while this
        screen is already on top, but nothing replays what landed before it
        opened. Without this, drilling into a failing object mid-run shows
        never-observed for the very check whose failure prompted the
        drill-in, until the entire run ends.

        A check-scope row's key is its ``check_id`` (see ``check_rows``),
        which is what the app's map is keyed by, so a result belonging to
        another object simply never matches a row here.

        Only ``overall`` is overlaid -- the same cell
        :meth:`apply_live_result` writes for a result arriving later, so a
        screen opened mid-run and one held open across the same run agree.
        The trailing-day cells need a day's full history, which only the
        store has, and the end-of-run refresh recomputes them.
        """
        from dbfresh.tui.app import DbfreshApp

        app = self.app
        assert isinstance(app, DbfreshApp)
        seeded: list[GridRow] = []
        for row in rows:
            live = app.live_status(row.key)
            seeded.append(row if live is None else replace(row, overall=live))
        return seeded

    def apply_live_result(self, result: Result) -> None:
        """Flip one check row's ``overall`` glyph the moment its
        ``Result`` arrives mid-run, without rebuilding the rest of the
        grid (``refresh_grid``'s full ``populate_grid`` rebuild, still
        used for the end-of-run authoritative refresh, would reset this
        screen's cursor on every single check that finishes).

        Called by ``DbfreshApp._apply_live_result`` only while this
        screen is the one on top. A no-op when ``result`` belongs to a
        different object -- a full run evaluates every object, this one
        included, so most results arriving here are not this screen's
        own -- or names a check not currently shown (also covers a
        result with no ``check_id``).
        """
        if result.source != self._source or result.object != self._object:
            return
        if result.check_id is None or result.check_id not in self._rows_by_key:
            return
        table = self.query_one(f"#{_DETAIL_GRID_ID}", DataTable)
        flash_cell(
            table,
            result.check_id,
            "overall",
            _status_cell(result.status),
            self,
            self._cell_flash_timers,
        )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        if event.row_key.value is None:
            return
        row = self._rows_by_key.get(event.row_key.value)
        if row is None or row.check is None:
            return
        self.app.push_screen(
            HistoryScreen(self._store, row.check, tz=self._tz)
        )

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        """Keep ``#check-detail-line`` in step with the cursor -- mirrors
        :meth:`on_data_table_row_selected`'s own row lookup, but updates the
        detail line instead of drilling into ``HistoryScreen``."""
        event.stop()
        self._sync_check_detail_line(event.row_key.value)

    def _current_row_key(self, table: DataTable) -> str | None:
        """The row key at ``table``'s current cursor position, or ``None``
        when the table has no rows (an object with no checks left)."""
        if table.row_count == 0:
            return None
        return table.coordinate_to_cell_key(
            table.cursor_coordinate
        ).row_key.value

    def _sync_check_detail_line(self, row_key: str | None) -> None:
        """Show or hide ``#check-detail-line`` for the check named by
        ``row_key``.

        Hidden for a header row (``row_key`` not in ``_rows_by_key``, never
        actually reached here since this grid has none), for a check never
        observed on this machine (``obs is None``), and for OK/SKIPPED
        (nothing to review) -- shown only for WARN/FAIL/ERROR, via
        :func:`_check_detail_text`.
        """
        line = self.query_one("#check-detail-line", Static)
        row = self._rows_by_key.get(row_key) if row_key is not None else None
        if row is None or row.check is None:
            line.display = False
            return
        obs = self._store.latest_observation(check_id(row.check))
        if obs is None or Status(obs["status"]) in (Status.OK, Status.SKIPPED):
            line.display = False
            return
        line.update(_check_detail_text(row.check, obs, self._tz))
        line.display = True

    def action_run_object(self) -> None:
        """Run only this object's checks (the "Run these checks" button's
        binding) -- distinct from the global 'r' (run every check), which
        stays bound to ``DbfreshApp.action_run_checks`` and keeps working
        unchanged from this screen.

        Delegates to ``DbfreshApp.run_object_checks``, which shares
        ``_run_checks_worker``'s exclusive worker group and its fresh-
        ``Store`` handling with a full run -- this screen doesn't run
        anything itself, it only asks the app to.
        """
        from dbfresh.tui.app import DbfreshApp

        app = self.app
        assert isinstance(app, DbfreshApp)
        app.run_object_checks(self._source, self._object)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == _RUN_OBJECT_BUTTON_ID:
            self.action_run_object()

    def action_dismiss_screen(self) -> None:
        self.dismiss()


_PRUNE_WORKER_GROUP = "store-prune"


class StoreScreen(Screen):
    """Observation-store size, retention, and a confirm-gated prune.

    The TUI's view onto ``dbfresh prune`` (``cli._prune_command``): shows
    the store's path, on-disk size, observation/run counts, and its
    configured ``retain_days`` (display only -- editing retention is out of
    scope here, see ``dbfresh.config.StoreConfig``), plus a "Prune now"
    button gated behind a two-press confirm -- a stray click must never
    drop observations.

    The prune itself runs on a worker thread against a *fresh* short-lived
    :class:`~dbfresh.store.Store` opened on ``store.path``, never on the
    app's own shared ``store`` connection -- see :meth:`_prune_worker`.
    """

    TITLE = "Store"

    BINDINGS = [Binding("escape", "dismiss_screen", "Back")]

    def __init__(self, store: Store, retain_days: int) -> None:
        super().__init__()
        self._store = store
        self._retain_days = retain_days

    def _info_text(self) -> str:
        return (
            f"path: {self._store.path}\n"
            f"size: {format_bytes(self._store.size_bytes())}\n"
            f"observations: {self._store.observation_count()}\n"
            f"runs: {self._store.run_count()}\n"
            f"retention: {self._retain_days} days"
        )

    def compose(self) -> ComposeResult:
        yield Header()
        confirm_row = Horizontal(
            Label("prune observations older than retention?", classes="hint"),
            Button("Confirm prune", id="store-prune-confirm-btn"),
            Button("Cancel", id="store-prune-cancel-btn"),
            id="store-prune-confirm-row",
        )
        confirm_row.display = False
        yield Static("Store", classes="screen-heading")
        yield Vertical(
            Static("Observation store", classes="section-title"),
            Static(self._info_text(), id="store-info"),
            Horizontal(Button("Prune now", id="store-prune-btn")),
            confirm_row,
            Static("", id="store-prune-result"),
            id="store-panel",
            classes="panel",
        )
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "store-prune-btn":
            self._arm_prune()
        elif button_id == "store-prune-confirm-btn":
            self._confirm_prune()
        elif button_id == "store-prune-cancel-btn":
            self._cancel_prune()

    def _arm_prune(self) -> None:
        """First press of "Prune now": reveal the confirm/cancel row rather
        than pruning outright -- a stray click must never drop
        observations."""
        self.query_one("#store-prune-confirm-row", Horizontal).display = True

    def _cancel_prune(self) -> None:
        self.query_one("#store-prune-confirm-row", Horizontal).display = False

    def _confirm_prune(self) -> None:
        self.query_one("#store-prune-confirm-row", Horizontal).display = False
        self.query_one("#store-prune-btn", Button).disabled = True
        self._prune_worker(self._store.path, self._retain_days)

    @work(
        thread=True,
        exclusive=True,
        group=_PRUNE_WORKER_GROUP,
        exit_on_error=False,
    )
    def _prune_worker(self, store_path: Path, retain_days: int) -> int:
        """Delete observations older than ``retain_days``, off the UI
        thread and off the app's own shared store connection.

        A check run started from Home writes to the app's ``self._store``
        connection from its own worker thread (see
        ``DbfreshApp._run_checks_worker``); pruning on that same connection
        from a second, concurrently-running worker would be two threads
        writing one sqlite3 connection at once. Opening a brand-new
        :class:`~dbfresh.store.Store` here instead -- its own connection,
        same file -- sidesteps that race entirely: WAL journaling plus the
        busy-timeout pragma (see ``Store.__init__``) already make two
        separate connections to the same store file safe to write from
        concurrently, exactly as they make two overlapping ``dbfresh run``
        processes safe today. Returns the deleted count; the main thread
        picks it up via :meth:`on_worker_state_changed`.
        """
        fresh = Store(store_path)
        try:
            return fresh.prune(retain_days)
        finally:
            fresh.close()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.group != _PRUNE_WORKER_GROUP:
            return
        if event.state == WorkerState.RUNNING:
            self.sub_title = "pruning…"
            return
        if event.state not in (
            WorkerState.SUCCESS,
            WorkerState.ERROR,
            WorkerState.CANCELLED,
        ):
            return

        self.sub_title = None
        self.query_one("#store-prune-btn", Button).disabled = False

        if event.state == WorkerState.CANCELLED:
            return
        if event.state == WorkerState.ERROR:
            self.notify(
                f"prune failed: {event.worker.error}", severity="error"
            )
            return

        deleted = event.worker.result
        assert deleted is not None
        self.query_one("#store-info", Static).update(self._info_text())
        result_text = f"pruned {deleted} observation(s) older than {self._retain_days} days"
        self.query_one("#store-prune-result", Static).update(result_text)
        self.notify(result_text)

    def action_dismiss_screen(self) -> None:
        self.app.pop_screen()


_HELP_BINDINGS_TEXT = """\
Global
  r        run checks
  R        reload config from disk
  ?        toggle this help
  q        quit

Home only
  c        configure
  p        report
  s        store
  f        toggle non-OK filter
  /        search by object (escape clears, enter keeps it)
  enter    open the selected object

Object detail
  O        run these checks
  enter    open the selected check's history

Any other screen
  escape   back -- Report, History, Object detail, Store
  escape   cancel -- Configure (discards anything not yet accepted)\
"""


class HelpScreen(ModalScreen[None]):
    """Every key binding plus the status-glyph legend, in one dismissible
    overlay reachable from any screen -- the one place the app-level '?'
    lives (see :meth:`~dbfresh.tui.app.DbfreshApp.action_help`).
    """

    BINDINGS = [
        Binding("escape", "dismiss_help", "Close"),
        Binding("question_mark", "dismiss_help", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-panel"):
            yield Static("Help", classes="screen-heading")
            yield Static(_HELP_BINDINGS_TEXT, id="help-bindings", markup=False)
            yield Static(status_legend(), id="help-legend")
            yield Static("escape or ? to close", classes="hint")

    def action_dismiss_help(self) -> None:
        self.dismiss(None)
