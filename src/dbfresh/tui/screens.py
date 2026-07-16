"""Report, History, and object-detail screens pushed from the Home grid."""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Static
from textual.worker import Worker, WorkerState

from dbfresh.checks import Check, check_id, parse_duration
from dbfresh.config import Config, load_config_tolerant
from dbfresh.configurator import (
    find_check_file,
    remove_check,
    rewrite_check_expectation,
)
from dbfresh.models import RunResult, Status
from dbfresh.report import reconstruct_run, render_digest, render_history
from dbfresh.store import Store, format_bytes
from dbfresh.tui.dashboard import (
    DrillDownTable,
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

    ``render_digest``'s own first line (the "DATA CHECK REPORT — ..."
    header, shared verbatim with the CLI's own digest) is bolded here on
    the TUI side only -- the plain-text CLI output itself is untouched.
    """
    plain = render_digest(run, tz=tz)
    blocks = iter(
        result
        for result in run.results
        if result.status not in (Status.OK, Status.SKIPPED)
    )
    lines: list[Text] = []
    for i, line in enumerate(plain.split("\n")):
        if i == 0:
            lines.append(Text(line, style="bold"))
            continue
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

    TITLE = "Report"

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
        yield Static("Report", classes="screen-heading")
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

    TITLE = "History"

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
        yield Static("History", classes="screen-heading")
        yield VerticalScroll(Static(text, id="history-text", markup=False))
        yield Footer()

    def action_dismiss_screen(self) -> None:
        self.app.pop_screen()


_DETAIL_GRID_ID = "object-detail-grid"
_RUN_OBJECT_BUTTON_ID = "detail-run-object-btn"

# An editable check's expect: operand gets an editing affordance only when
# it's a shape this screen (and rewrite_check_expectation's callers) knows
# how to build a form for: one scalar Input, or -- unlike Configure's own
# in-Propose editing (dbfresh.tui.configure._NON_EDITABLE_OPERATORS) -- a
# between's [lo, hi] pair via two Inputs. vs_previous (a nested guard
# mapping) and schema's unchanged (no operand at all) still show read-only;
# a bespoke form for either is out of scope here.
_NON_EDITABLE_OPERATORS = frozenset({"unchanged", "vs_previous"})


class ObjectDetailScreen(Screen[bool]):
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

    Below the grid, an "Edit checks" panel lists this object's checks again,
    each with a threshold-editing and a delete affordance -- the
    connection-free counterpart to Configure's in-Propose existing-check
    editing (:meth:`~dbfresh.tui.configure.ConfigureScreen._save_existing`):
    this screen already scopes to one object's checks and needs no source
    adapter to mutate config YAML on disk. Dismisses with ``True`` when any
    edit or delete actually wrote to disk (so Home reloads the config and
    refreshes the dashboard, mirroring
    :meth:`~dbfresh.tui.app.DbfreshApp._on_configure_dismissed`), ``False``
    otherwise.
    """

    TITLE = "Object detail"

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Back"),
        Binding("O", "run_object", "Run this object"),
    ]

    # Textual's default auto-focus (App.AUTO_FOCUS = "*") lands on the
    # first focusable widget in DOM order -- since the "Run this object"
    # button above the grid (see compose()) is focusable too, without this
    # it would steal initial focus from the grid instead, breaking Enter's
    # row-drill-in as the screen's own opening behavior.
    AUTO_FOCUS = f"#{_DETAIL_GRID_ID}"

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
        self._edit_checks: list[Check] = []
        self._edit_value_inputs: list[Input | None] = []
        self._edit_lo_inputs: list[Input | None] = []
        self._edit_hi_inputs: list[Input | None] = []
        self._config_changed = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Object detail", classes="screen-heading")
        yield Static(f"{self._source}.{self._object}", id="object-detail-heading")
        yield Horizontal(Button("Run this object", id=_RUN_OBJECT_BUTTON_ID))
        yield DrillDownTable(
            id=_DETAIL_GRID_ID,
            cursor_type="row",
            zebra_stripes=True,
            cell_padding=2,
            # See DbfreshApp.compose's dashboard-grid DataTable -- same
            # reason: keep each cell's own status color on the cursor row.
            cursor_foreground_priority="renderable",
        )
        yield Static(status_legend(), id="status-legend")
        yield VerticalScroll(
            Vertical(
                Static("Edit checks", classes="section-title"),
                Vertical(id="detail-edit-checks"),
                id="detail-edit-section",
                classes="panel",
            ),
            id="detail-edit-scroll",
        )
        yield Footer()

    async def on_mount(self) -> None:
        self.refresh_grid()
        await self._mount_edit_checks()

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

    def action_run_object(self) -> None:
        """Run only this object's checks (the "Run this object" button's
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

    async def _mount_edit_checks(self) -> None:
        """One row per this object's checks, each with a threshold-editing
        affordance (when the operator supports one) and a delete
        affordance (always) -- rebuilt from ``self._config`` on every
        mount and after every mutation, so it never drifts from what
        :meth:`refresh_grid` is showing above it.

        ``remove_children`` only schedules removal (it returns an
        awaitable, see ``Widget.remove_children``) -- without awaiting it
        here, a second mutation's remount can race the first's still-
        pending removal and collide on this screen's stable per-check
        widget ids (``detail-save-0`` etc.), raising ``DuplicateIds``.
        """
        container = self.query_one("#detail-edit-checks", Vertical)
        await container.remove_children()
        self._edit_checks = [
            c
            for c in self._config.checks
            if c.source == self._source and c.object == self._object
        ]
        self._edit_value_inputs = []
        self._edit_lo_inputs = []
        self._edit_hi_inputs = []
        if not self._edit_checks:
            container.mount(Static("(no checks left for this object)"))
            return
        for i, check in enumerate(self._edit_checks):
            container.mount(*self._build_edit_widgets(i, check))

    def _build_edit_widgets(self, i: int, check: Check) -> list[Horizontal]:
        """The widgets for one check's edit row -- mounted as flat siblings
        into ``#detail-edit-checks`` (mirroring
        :meth:`~dbfresh.tui.configure.ConfigureScreen._mount_existing_checks`'s
        own flat mounting) rather than wrapped in a further container, so
        each row's height comes only from :data:`ObjectDetailScreen`'s own
        ``Horizontal { height: auto; }`` rule in app.tcss, the same rule
        Configure's per-check rows already rely on.

        Always ends with a Delete button and a hidden confirm/cancel row --
        every check is deletable regardless of whether its expectation has
        an editable operand. Row text uses ``Label``, not ``Static`` --
        plain ``Static`` declares no width of its own, and inside a
        ``Horizontal`` that leaves Textual sizing it to the *rest* of the
        row's available space instead of to its own content, which would
        push every widget after it (Input, Save, Delete) out past the
        row's overflow-hidden bounds. ``Label`` (a ``Static`` subclass)
        declares ``width: auto`` explicitly, which sizes it to content as
        intended.
        """
        label = check_label(check)
        confirm_row = Horizontal(
            Label("delete this check permanently?", classes="hint"),
            Button("Confirm delete", id=f"detail-confirm-{i}"),
            Button("Cancel", id=f"detail-cancel-{i}"),
            id=f"detail-confirm-row-{i}",
        )
        confirm_row.display = False

        if check.expect is None:
            self._edit_value_inputs.append(None)
            self._edit_lo_inputs.append(None)
            self._edit_hi_inputs.append(None)
            edit_row = Horizontal(
                Label(label), Button("Delete", id=f"detail-delete-{i}")
            )
        elif check.expect.operator == "between":
            lo, hi = check.expect.operand
            lo_input = Input(value=str(lo), id=f"detail-lo-{i}")
            hi_input = Input(value=str(hi), id=f"detail-hi-{i}")
            self._edit_value_inputs.append(None)
            self._edit_lo_inputs.append(lo_input)
            self._edit_hi_inputs.append(hi_input)
            edit_row = Horizontal(
                Label(f"{label} (between):"),
                lo_input,
                Label("and"),
                hi_input,
                Button("Save", id=f"detail-save-{i}"),
                Button("Delete", id=f"detail-delete-{i}"),
            )
        elif check.expect.operator in _NON_EDITABLE_OPERATORS:
            self._edit_value_inputs.append(None)
            self._edit_lo_inputs.append(None)
            self._edit_hi_inputs.append(None)
            edit_row = Horizontal(
                Label(f"{label}: {check.expect.describe()}"),
                Button("Delete", id=f"detail-delete-{i}"),
            )
        else:
            operand = check.expect.operand
            current = operand if isinstance(operand, str) else str(operand)
            value_input = Input(value=current, id=f"detail-value-{i}")
            self._edit_value_inputs.append(value_input)
            self._edit_lo_inputs.append(None)
            self._edit_hi_inputs.append(None)
            edit_row = Horizontal(
                Label(f"{label} ({check.expect.operator}):"),
                value_input,
                Button("Save", id=f"detail-save-{i}"),
                Button("Delete", id=f"detail-delete-{i}"),
            )

        return [edit_row, confirm_row]

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == _RUN_OBJECT_BUTTON_ID:
            self.action_run_object()
        elif button_id.startswith("detail-save-"):
            await self._save_edit(int(button_id.removeprefix("detail-save-")))
        elif button_id.startswith("detail-delete-"):
            self._arm_delete(int(button_id.removeprefix("detail-delete-")))
        elif button_id.startswith("detail-confirm-"):
            await self._confirm_delete(int(button_id.removeprefix("detail-confirm-")))
        elif button_id.startswith("detail-cancel-"):
            self._cancel_delete(int(button_id.removeprefix("detail-cancel-")))

    def _arm_delete(self, i: int) -> None:
        """First press of Delete: reveal the confirm/cancel row rather than
        deleting outright -- a stray click must never remove a check."""
        self.query_one(f"#detail-confirm-row-{i}", Horizontal).display = True

    def _cancel_delete(self, i: int) -> None:
        self.query_one(f"#detail-confirm-row-{i}", Horizontal).display = False

    async def _confirm_delete(self, i: int) -> None:
        check = self._edit_checks[i]
        cid = check_id(check)
        try:
            remove_check(self._config_path, cid)
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return
        self._config_changed = True
        self.notify(f"deleted {check_label(check)}")
        await self._reload_and_refresh()

    def _parse_scalar_edit(
        self, check: Check, raw_value: str
    ) -> tuple[dict | None, str | None]:
        """Parse a single-scalar Input's text into a new ``expect:`` dict
        for ``check``, preserving its operator -- only the value beside it
        is editable. Mirrors
        :meth:`~dbfresh.tui.configure.ConfigureScreen._save_existing`."""
        assert check.expect is not None
        raw_value = raw_value.strip()
        if check.metric == "freshness":
            try:
                parse_duration(raw_value)
            except ValueError as exc:
                return None, f"invalid max lag: {exc}"
            return {check.expect.operator: raw_value}, None
        try:
            value = float(raw_value)
        except ValueError:
            return None, f"not a number: {raw_value!r}"
        return {check.expect.operator: value}, None

    def _parse_between_edit(
        self, raw_lo: str, raw_hi: str
    ) -> tuple[dict | None, str | None]:
        """Parse a between row's two Inputs into a new ``{between: [lo,
        hi]}`` dict -- both sides must parse as numbers, and lo must not
        exceed hi (mirrors :meth:`~dbfresh.checks.Expectation.evaluate`'s
        own ``lo <= value <= hi`` reading of the pair)."""
        try:
            lo = float(raw_lo.strip())
            hi = float(raw_hi.strip())
        except ValueError:
            return None, f"between requires two numbers, got {raw_lo!r} and {raw_hi!r}"
        if lo > hi:
            return None, f"between requires lo <= hi, got [{lo}, {hi}]"
        return {"between": [lo, hi]}, None

    async def _save_edit(self, i: int) -> None:
        check = self._edit_checks[i]
        assert check.expect is not None

        lo_input = self._edit_lo_inputs[i]
        hi_input = self._edit_hi_inputs[i]
        if lo_input is not None and hi_input is not None:
            new_expect, error = self._parse_between_edit(lo_input.value, hi_input.value)
        else:
            value_input = self._edit_value_inputs[i]
            assert value_input is not None
            new_expect, error = self._parse_scalar_edit(check, value_input.value)
        if error is not None:
            self.notify(error, title="Invalid check value", severity="error")
            return
        assert new_expect is not None

        cid = check_id(check)
        target = find_check_file(self._config_path, cid)
        if target is None:
            self.notify(f"could not locate check {cid} on disk", severity="error")
            return
        rewrite_check_expectation(target, cid, new_expect)
        self._config_changed = True
        self.notify(f"saved {check_label(check)}")
        await self._reload_and_refresh()

    async def _reload_and_refresh(self) -> None:
        """Reload config from disk after a write this screen just made, and
        re-render both the grid and the edit panel from it -- so an edit or
        delete is reflected here immediately rather than only once the user
        pops back to Home and drills in again."""
        try:
            self._config, _missing = load_config_tolerant(self._config_path)
        except Exception as exc:
            self.notify(
                f"config reload failed after write: {exc}",
                title="Reload failed",
                severity="error",
                timeout=10,
            )
            return
        self.refresh_grid()
        await self._mount_edit_checks()

    def action_dismiss_screen(self) -> None:
        self.dismiss(self._config_changed)


_PRUNE_WORKER_GROUP = "store-prune"


class StoreScreen(Screen):
    """Observation-store size, retention, and a confirm-gated prune.

    The TUI's view onto ``dbfresh prune`` (``cli._prune_command``): shows
    the store's path, on-disk size, observation/run counts, and its
    configured ``retain_days`` (display only -- editing retention is out of
    scope here, see ``dbfresh.config.StoreConfig``), plus a "Prune now"
    button gated behind the same two-press confirm
    :class:`ObjectDetailScreen`'s delete-check uses.

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
        than pruning outright -- a stray click must never drop observations
        (mirrors ``ObjectDetailScreen``'s delete-check confirm)."""
        self.query_one("#store-prune-confirm-row", Horizontal).display = True

    def _cancel_prune(self) -> None:
        self.query_one("#store-prune-confirm-row", Horizontal).display = False

    def _confirm_prune(self) -> None:
        self.query_one("#store-prune-confirm-row", Horizontal).display = False
        self.query_one("#store-prune-btn", Button).disabled = True
        self._prune_worker(self._store.path, self._retain_days)

    @work(thread=True, exclusive=True, group=_PRUNE_WORKER_GROUP, exit_on_error=False)
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
            self.notify(f"prune failed: {event.worker.error}", severity="error")
            return

        deleted = event.worker.result
        assert deleted is not None
        self.query_one("#store-info", Static).update(self._info_text())
        result_text = (
            f"pruned {deleted} observation(s) older than {self._retain_days} days"
        )
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
  o        toggle worst-first sort
  enter    open the selected object

Object detail
  O        run this object's checks
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
