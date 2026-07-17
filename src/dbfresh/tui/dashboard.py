"""Build the Home status grid and object drill-in grid from config + store.

Two scopes share one row shape (:class:`GridRow`) and one renderer
(:func:`populate_grid`): the Home screen's rows are one per source.object;
the drill-in (``ObjectDetailScreen``) rows are one per check within a
single object. Each row carries an "overall" (latest observation) status
plus a trailing 7-day trend, bucketed to the latest status observed each
calendar day -- the same "latest, not worst" rule ``overall`` already
uses -- so a same-day recovery never contradicts ``overall``. A day that
also saw a worse status earlier gets a small trailing marker rather than
losing that information outright.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo

from rich.text import Text
from textual.binding import Binding
from textual.widgets import DataTable

from dbfresh.checks import Check, check_id
from dbfresh.config import Config
from dbfresh.models import Status, worst_status
from dbfresh.store import Store

_TRAILING_DAYS = 7
# Comfortably covers 7 calendar days even at a few runs/day; a sparser
# history (one run/day) needs far fewer observations than this.
_HISTORY_FETCH_LIMIT = 50

# Catppuccin Macchiato hexes (see dbfresh.tui.app.tcss for the same
# palette wired into the TUI's CSS side) -- OK/WARN/FAIL/ERROR are bold so
# they read as "known, active status"; SKIPPED and never-observed are not,
# so the two active/inactive groups are distinct at a glance in addition
# to their own colors.
_STATUS_STYLE: dict[Status | None, str] = {
    Status.OK: "bold #a6da95",  # green
    Status.WARN: "bold #eed49f",  # yellow
    Status.FAIL: "bold #ed8796",  # red
    Status.ERROR: "bold #8aadf4",  # blue -- distinct from FAIL
    Status.SKIPPED: "#8bd5ca",  # teal -- distinct from never-observed
    None: "#6e738d",  # overlay0 -- muted, never observed
}

# A day/overall cell is one glyph, not a word -- the grid's whole point is
# fitting many rows/columns in limited width. FAIL ("bad data": the check
# ran and the value failed its expectation) and ERROR ("source unreachable":
# the check never got a value to compare) are different failure modes with
# different fixes, so they get both a distinct glyph and a distinct color
# rather than sharing one. SKIPPED (deliberately not evaluated) and unknown
# (never observed) are both muted, but SKIPPED keeps a hint of color so the
# two don't read as the same "nothing to see here" grey.
_STATUS_GLYPH: dict[Status | None, str] = {
    Status.OK: "✓",
    Status.WARN: "!",
    Status.FAIL: "✗",
    Status.ERROR: "⊘",
    Status.SKIPPED: "–",
    None: "·",
}

_STATUS_LABEL: dict[Status | None, str] = {
    Status.OK: "ok",
    Status.WARN: "warn",
    Status.FAIL: "fail",
    Status.ERROR: "error (unreachable)",
    Status.SKIPPED: "skipped",
    None: "never observed",
}


def status_glyph(status: Status | None) -> str:
    """The single-character glyph for ``status`` (``None`` = never
    observed) -- shared by the status grid, its legend, and the Report
    screen, so all three read the same glyph the same way."""
    return _STATUS_GLYPH[status]


def status_style(status: Status | None) -> str:
    """The Rich style for ``status`` (``None`` = never observed) -- shared
    by the status grid, its legend, and the Report screen."""
    return _STATUS_STYLE[status]


def status_legend() -> Text:
    """A compact glyph legend, one entry per status plus never-observed --
    the same order the grid's own severity reads worst to least severe,
    with the two non-severity states (skipped, never observed) last. A
    trailing line explains the day cells' own marker (see
    :func:`_day_marker`), which this per-status legend doesn't otherwise
    cover."""
    order = [
        Status.OK,
        Status.WARN,
        Status.FAIL,
        Status.ERROR,
        Status.SKIPPED,
        None,
    ]
    text = Text()
    for status in order:
        if text.plain:
            text.append("   ")
        text.append(status_glyph(status), style=status_style(status))
        text.append(f" {_STATUS_LABEL[status]}")
    text.append("\n")
    text.append(
        "a trailing ✗/!/· marks a worse status earlier that day (fail / warn / error)"
    )
    return text


# Status label used only by last_run_line's one-line summary -- kept
# separate from _STATUS_LABEL (the grid legend's wording, e.g. "error
# (unreachable)") because that wording is too verbose for a one-line
# summary, and because that mapping's None ("never observed") case never
# applies here: every row counted comes from one already-completed run.
_RUN_STATUS_WORD: dict[Status, str] = {
    Status.OK: "ok",
    Status.WARN: "warned",
    Status.FAIL: "failed",
    Status.ERROR: "unreachable",
    Status.SKIPPED: "skipped",
}


def last_run_line(store: Store, tz: tzinfo | None) -> str | None:
    """A one-line "last run: <time> · N checks · ..." summary of the most
    recent completed run, or ``None`` when no run has finished yet -- the
    caller hides the line entirely in that case rather than rendering a
    blank one.

    Time comes from the run's own ``finished_at`` (displayed in ``tz``);
    counts come from that run's own observations
    (:meth:`~dbfresh.store.Store.observations_for_run`) rather than the
    dashboard grid's per-check latest status, so the line reflects exactly
    what that one run produced, not the current state of every check.
    """
    run = store.latest_run()
    if run is None:
        return None
    observations = store.observations_for_run(run["run_id"])
    counts = dict.fromkeys(Status, 0)
    for obs in observations:
        counts[Status(obs["status"])] += 1
    finished_at = datetime.fromisoformat(run["finished_at"])
    when = finished_at.astimezone(tz) if tz is not None else finished_at
    parts = [
        f"{counts[status]} {word}"
        for status, word in _RUN_STATUS_WORD.items()
        if status != Status.OK and counts[status]
    ]
    summary = " · ".join(parts) if parts else "all ok"
    return (
        f"last run: {when.strftime('%Y-%m-%d %H:%M')} · "
        f"{len(observations)} checks · {summary}"
    )


def check_label(check: Check) -> str:
    """The label shown for one check's row.

    Unlike the old nested tree (where a column/key node already grouped
    same-column checks), this grid is flat, so a bare metric name like
    'null_rate' would be ambiguous with more than one null_rate check on
    the same object -- the column/key is appended in parens to disambiguate
    whenever the check has one; a table-level check (row_count, schema, an
    assertion) has none and stays bare.
    """
    if check.assert_ is not None:
        return f"assert {check.assert_}"
    if check.assert_sql is not None:
        return f"assert_sql {check.assert_sql}"
    label = check.metric or "check"
    context = check.column or check.key
    return f"{label} ({context})" if context else label


def _worst_or_unknown(statuses: list[Status]) -> Status | None:
    """The worst known status, or ``None`` when there are no known statuses.

    A row whose only known statuses are SKIPPED rolls up to SKIPPED rather
    than OK, even though the two share severity rank 0 in
    :func:`~dbfresh.models.worst_status` (which exit-code aggregation
    depends on). A mix of OK and SKIPPED still rolls up to OK.
    """
    if not statuses:
        return None
    if all(status == Status.SKIPPED for status in statuses):
        return Status.SKIPPED
    return worst_status(statuses)


# Priority order for _day_marker when a day's statuses qualify more than
# one candidate: FAIL outranks WARN outranks ERROR, deliberately not the
# same order as worst_status's own severity (where ERROR outranks FAIL).
# FAIL is a real data failure (the check ran and the value failed); WARN a
# softer data issue; ERROR is usually a config mistake, an unreachable
# source, or a comparison check's first run with no baseline yet -- not a
# data failure -- so it reads neutral and loses priority to either.
_MARKER_PRIORITY = (Status.FAIL, Status.WARN, Status.ERROR)


def _day_marker(latest: Status | None, all_statuses: list[Status]) -> Status | None:
    """Whether ``all_statuses`` (every status a day saw) contains something
    strictly worse than ``latest`` (the day's current, latest-observed
    status) -- and if so, which one to flag when more than one qualifies.

    "Worse" is decided by :func:`~dbfresh.models.worst_status`, but the
    candidate returned on a tie between multiple qualifying statuses
    follows ``_MARKER_PRIORITY``, not raw severity -- see its own
    docstring. A day that never got worse than where it ended up (an
    unbroken OK day, or a still-failing day whose worst status equals its
    latest one) has no marker.
    """
    if latest is None:
        return None
    for candidate in _MARKER_PRIORITY:
        if (
            candidate in all_statuses
            and candidate != latest
            and worst_status([candidate, latest]) == candidate
        ):
            return candidate
    return None


def trailing_dates(today: date, days: int = _TRAILING_DAYS) -> list[date]:
    """The last ``days`` calendar dates ending on (and including) ``today``,
    oldest first -- so the grid reads left (past) to right (present)."""
    return [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]


def bucket_by_day(
    rows: list[dict], dates: list[date], tz: tzinfo | None
) -> dict[date, tuple[Status | None, list[Status]]]:
    """Each of ``dates`` mapped to (latest-of-day status, every status seen
    that day), from :meth:`~dbfresh.store.Store.history` rows. "Latest" is
    decided by each row's own ``observed_at``, not the order ``rows``
    arrives in -- callers don't guarantee any particular order. A date
    among ``dates`` with no matching observation maps to ``(None, [])``.
    ``rows`` outside ``dates`` (older than the trailing window) are
    ignored, not an error -- callers fetch a generous history limit to
    comfortably cover the window even at several runs/day, which routinely
    includes older rows too.
    """
    by_date: dict[date, list[tuple[datetime, Status]]] = {d: [] for d in dates}
    for row in rows:
        when = datetime.fromisoformat(row["observed_at"])
        observed_date = when.astimezone(tz).date() if tz else when.date()
        if observed_date in by_date:
            by_date[observed_date].append((when, Status(row["status"])))
    result: dict[date, tuple[Status | None, list[Status]]] = {}
    for d, entries in by_date.items():
        if not entries:
            result[d] = (None, [])
            continue
        latest = max(entries, key=lambda entry: entry[0])[1]
        result[d] = (latest, [status for _, status in entries])
    return result


class DrillDownTable(DataTable):
    """A status-grid ``DataTable`` whose Enter key is discoverable.

    Plain ``DataTable`` already binds Enter to ``select_cursor`` (which is
    what fires the ``RowSelected`` message both status grids drill into a
    row on), but ships that binding with ``show=False`` -- Textual's own
    default -- so the footer never mentioned it. Re-declaring the same key
    with the same action and ``show=True`` only changes whether the footer
    advertises it; the row-selection behavior itself is untouched. Used by
    both the Home grid (drills into :class:`~dbfresh.tui.screens.
    ObjectDetailScreen`) and the drill-in grid (drills into
    :class:`~dbfresh.tui.screens.HistoryScreen`), so one shared label
    ("Open") covers both destinations.
    """

    BINDINGS = [Binding("enter", "select_cursor", "Open", show=True)]


@dataclass(frozen=True)
class GridRow:
    """One row of a status grid, at either scope (object or check).

    ``source``/``object`` are set for an object-scope row (Home screen) --
    what :class:`~dbfresh.tui.screens.ObjectDetailScreen` drills into on
    selection. ``check`` is set for a check-scope row (the drill-in) --
    what ``HistoryScreen`` opens on selection. A row never has both.

    Each entry in ``days`` is (latest-of-day status, marker status) -- see
    :func:`bucket_by_day` and :func:`_day_marker`.
    """

    key: str
    label: str
    overall: Status | None
    days: list[tuple[Status | None, Status | None]]
    source: str | None = None
    object: str | None = None
    check: Check | None = None


def _rollup(
    checks: list[Check], store: Store, dates: list[date], tz: tzinfo | None
) -> tuple[Status | None, list[tuple[Status | None, Status | None]]]:
    """Overall (latest) and per-day (latest-of-day plus marker) rollup
    across ``checks`` -- a single-check list (the drill-in scope) rolls up
    to exactly that check's own statuses; a multi-check list (the Home
    scope) rolls up across all of an object's checks.

    Each day's tuple is (latest-of-day, marker): latest-of-day is the
    worst of each check's own latest-of-day status, i.e. the object's
    current state that day; marker is computed from that value plus every
    status any of ``checks`` saw that day, so a same-day recovery on one
    check still surfaces as a marker even though it no longer moves
    latest-of-day itself.
    """
    overall_statuses: list[Status] = []
    day_latest_buckets: list[list[Status]] = [[] for _ in dates]
    day_all_buckets: list[list[Status]] = [[] for _ in dates]
    for check in checks:
        cid = check_id(check)
        latest = store.latest_observation(cid)
        if latest is not None:
            overall_statuses.append(Status(latest["status"]))
        history = store.history(cid, limit=_HISTORY_FETCH_LIMIT)
        per_day = bucket_by_day(history, dates, tz)
        for i, day in enumerate(dates):
            day_latest, day_statuses = per_day[day]
            if day_latest is not None:
                day_latest_buckets[i].append(day_latest)
            day_all_buckets[i].extend(day_statuses)
    overall = _worst_or_unknown(overall_statuses)
    days: list[tuple[Status | None, Status | None]] = []
    for i in range(len(dates)):
        day_latest = _worst_or_unknown(day_latest_buckets[i])
        days.append((day_latest, _day_marker(day_latest, day_all_buckets[i])))
    return overall, days


def object_rows(
    config: Config, store: Store, today: date, tz: tzinfo | None
) -> list[GridRow]:
    """The Home screen's rows: one per source.object, sorted by (source,
    object) -- reuses the existing worst-status rollup regardless of how
    many checks the object has."""
    by_source: dict[str, dict[str, list[Check]]] = {}
    for check in config.checks:
        by_source.setdefault(check.source, {}).setdefault(check.object, []).append(
            check
        )

    dates = trailing_dates(today)
    rows: list[GridRow] = []
    for source_name in sorted(by_source):
        for object_name in sorted(by_source[source_name]):
            checks = by_source[source_name][object_name]
            overall, days = _rollup(checks, store, dates, tz)
            rows.append(
                GridRow(
                    key=f"{source_name}\x1f{object_name}",
                    label=f"{source_name}.{object_name}",
                    overall=overall,
                    days=days,
                    source=source_name,
                    object=object_name,
                )
            )
    return rows


# Where worst-first sort places each status, low to high (lower sorts
# first -- i.e. more severe). Mirrors dbfresh.models.worst_status's own
# OK/SKIPPED < WARN < FAIL < ERROR severity ordering (models._SEVERITY),
# reversed so the most severe row rises to the top. SKIPPED and never-
# observed are not severity outcomes (a SKIPPED or unobserved check never
# failed anything) -- both sort after every real status, in the same
# relative order status_legend() already lists them in, rather than being
# ranked among OK/WARN/FAIL/ERROR.
_SORT_SEVERITY: dict[Status | None, int] = {
    Status.ERROR: 0,
    Status.FAIL: 1,
    Status.WARN: 2,
    Status.OK: 3,
    Status.SKIPPED: 4,
    None: 5,
}


@dataclass
class GridView:
    """The Home grid's active view controls -- held on the app
    (``DbfreshApp._view``) and funneled through :meth:`apply` every time
    the grid is (re)built (``refresh_dashboard``, and each toggle/keystroke
    that changes one of these), so a run or a config reload always
    re-renders through the same filter/search/sort the user last set
    rather than silently resetting it.
    """

    hide_ok: bool = False
    search: str = ""
    worst_first: bool = False

    @property
    def active(self) -> bool:
        """Whether any control is narrowing or reordering the default
        view -- callers use this to decide whether to show an indicator
        alongside the grid."""
        return self.hide_ok or bool(self.search.strip()) or self.worst_first

    def apply(self, rows: list[GridRow]) -> list[GridRow]:
        """``rows`` (:func:`object_rows`'s own source/object order),
        filtered and/or reordered per the current controls. Filtering runs
        first, narrowing the candidates; sorting then reorders whatever's
        left. Every control at its default returns ``rows`` unchanged, in
        ``object_rows``'s own order -- computing the rows themselves is
        untouched by this, it only ever narrows or reorders that output.
        """
        visible = rows
        if self.hide_ok:
            visible = [row for row in visible if row.overall != Status.OK]
        needle = self.search.strip().lower()
        if needle:
            visible = [row for row in visible if needle in row.label.lower()]
        if self.worst_first:
            # enumerate before sorting so ties fall back to the incoming
            # (source, object) order rather than an unstable/arbitrary one.
            ranked = sorted(
                enumerate(visible),
                key=lambda pair: (_SORT_SEVERITY[pair[1].overall], pair[0]),
            )
            visible = [row for _, row in ranked]
        return visible

    def status_text(self) -> str:
        """A short "what's active" summary for the Home footer/status line
        -- empty when :attr:`active` is ``False``, so the caller can hide
        the indicator entirely rather than show a blank one."""
        parts = []
        if self.hide_ok:
            parts.append("non-OK only")
        if self.search.strip():
            parts.append(f"search {self.search.strip()!r}")
        if self.worst_first:
            parts.append("worst-first")
        return " · ".join(parts)


def check_rows(
    source: str,
    object_: str,
    config: Config,
    store: Store,
    today: date,
    tz: tzinfo | None,
) -> list[GridRow]:
    """The drill-in rows for one source.object: one per check, in config
    order -- the same [overall, trailing days] shape as :func:`object_rows`,
    scoped to a single check per row instead of rolled up across many."""
    checks = [c for c in config.checks if c.source == source and c.object == object_]
    dates = trailing_dates(today)
    rows: list[GridRow] = []
    for check in checks:
        overall, days = _rollup([check], store, dates, tz)
        rows.append(
            GridRow(
                key=check_id(check),
                label=check_label(check),
                overall=overall,
                days=days,
                check=check,
            )
        )
    return rows


def _status_cell(status: Status | None) -> Text:
    text = Text(status_glyph(status), style=status_style(status))
    text.justify = "center"
    return text


def _marker_glyph_and_style(marker: Status) -> tuple[str, str]:
    """The (glyph, style) a day cell's trailing marker renders with.

    FAIL and WARN reuse their own status glyph/style verbatim -- a marker
    for either is exactly as alarming as the status itself. ERROR instead
    borrows the never-observed glyph/style (a muted "·", not ERROR's own
    blue "⊘"): here it's usually a config mistake, an unreachable source,
    or a comparison check's first run with no baseline -- not a data
    failure -- so it deliberately reads as neutral rather than alarming.
    """
    if marker == Status.ERROR:
        return status_glyph(None), status_style(None)
    return status_glyph(marker), status_style(marker)


def _day_cell(latest: Status | None, marker: Status | None) -> Text:
    """One trailing-day grid cell: the day's latest status glyph, plus a
    trailing marker glyph when the day also saw something worse (see
    :func:`_day_marker`).

    The day column is a fixed width of 3. A marker-less cell is a single
    centered glyph, like the ``overall`` column. When a marker is present, a
    leading space keeps the primary glyph at that same center (position 1)
    and lets the marker sit to its right -- centering the two-character
    ``glyph+marker`` pair instead would pull the primary glyph left of
    center and break its vertical alignment with the marker-less cells above
    and below it.
    """
    if marker is None:
        text = Text(status_glyph(latest), style=status_style(latest))
        text.justify = "center"
        return text
    text = Text(" ")
    text.append(status_glyph(latest), style=status_style(latest))
    glyph, style = _marker_glyph_and_style(marker)
    text.append(glyph, style=style)
    text.justify = "left"
    return text


def populate_grid(
    table: DataTable, rows: list[GridRow], today: date, label_header: str
) -> None:
    """(Re)populate ``table`` from ``rows``. Safe to call repeatedly: clears
    both rows and columns first, since the trailing-day column headers
    themselves shift by one day if two calls straddle midnight.

    ``label_header`` names the first column -- "object" at the Home scope,
    "check" at the drill-in scope -- since the two share this one renderer
    but the label column holds a different kind of thing at each scope.
    """
    table.clear(columns=True)
    table.add_column(label_header, key="label")
    # Explicit widths (content width, before cell_padding is added on top
    # by the table) rather than auto-sizing to the header text -- the
    # overall column only ever holds a single glyph, and a day column at
    # most a glyph plus a one-character marker, so auto-sizing either to
    # its own 3-7 character header left just the table's cell_padding as
    # breathing room around the glyph.
    table.add_column("overall", key="overall", width=7)
    for day in trailing_dates(today):
        table.add_column(day.strftime("%a"), key=day.isoformat(), width=3)
    for row in rows:
        cells = [row.label, _status_cell(row.overall)]
        cells.extend(_day_cell(latest, marker) for latest, marker in row.days)
        table.add_row(*cells, key=row.key)
