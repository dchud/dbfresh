"""Build the Home status grid and object drill-in grid from config + store.

Two scopes share one row shape (:class:`GridRow`) and one renderer
(:func:`populate_grid`): the Home screen's rows are one per source.object;
the drill-in (``ObjectDetailScreen``) rows are one per check within a
single object. Each row carries an "overall" (latest observation) status
plus a trailing 7-day trend, bucketed to the worst status observed each
calendar day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo

from rich.text import Text
from textual.widgets import DataTable

from dbfresh.checks import Check, check_id
from dbfresh.config import Config
from dbfresh.models import Status, worst_status
from dbfresh.store import Store

_TRAILING_DAYS = 7
# Comfortably covers 7 calendar days even at a few runs/day; a sparser
# history (one run/day) needs far fewer observations than this.
_HISTORY_FETCH_LIMIT = 50

_STATUS_STYLE: dict[Status | None, str] = {
    Status.OK: "bold green",
    Status.WARN: "bold yellow",
    Status.FAIL: "bold red",
    Status.ERROR: "bold blue",
    Status.SKIPPED: "dim cyan",
    None: "dim",
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
    with the two non-severity states (skipped, never observed) last."""
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
    return text


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


def trailing_dates(today: date, days: int = _TRAILING_DAYS) -> list[date]:
    """The last ``days`` calendar dates ending on (and including) ``today``,
    oldest first -- so the grid reads left (past) to right (present)."""
    return [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]


def bucket_by_day(
    rows: list[dict], dates: list[date], tz: tzinfo | None
) -> dict[date, Status | None]:
    """The worst status observed on each of ``dates``, from
    :meth:`~dbfresh.store.Store.history` rows. A date among ``dates`` with
    no matching observation maps to ``None``. ``rows`` outside ``dates``
    (older than the trailing window) are ignored, not an error -- callers
    fetch a generous history limit to comfortably cover the window even at
    several runs/day, which routinely includes older rows too.
    """
    by_date: dict[date, list[Status]] = {d: [] for d in dates}
    for row in rows:
        when = datetime.fromisoformat(row["observed_at"])
        observed_date = when.astimezone(tz).date() if tz else when.date()
        if observed_date in by_date:
            by_date[observed_date].append(Status(row["status"]))
    return {d: _worst_or_unknown(statuses) for d, statuses in by_date.items()}


@dataclass(frozen=True)
class GridRow:
    """One row of a status grid, at either scope (object or check).

    ``source``/``object`` are set for an object-scope row (Home screen) --
    what :class:`~dbfresh.tui.screens.ObjectDetailScreen` drills into on
    selection. ``check`` is set for a check-scope row (the drill-in) --
    what ``HistoryScreen`` opens on selection. A row never has both.
    """

    key: str
    label: str
    overall: Status | None
    days: list[Status | None]
    source: str | None = None
    object: str | None = None
    check: Check | None = None


def _rollup(
    checks: list[Check], store: Store, dates: list[date], tz: tzinfo | None
) -> tuple[Status | None, list[Status | None]]:
    """Overall (latest) and per-day (trailing history) rollup across
    ``checks`` -- a single-check list (the drill-in scope) rolls up to
    exactly that check's own statuses; a multi-check list (the Home scope)
    rolls up across all of an object's checks."""
    overall_statuses: list[Status] = []
    day_buckets: list[list[Status]] = [[] for _ in dates]
    for check in checks:
        cid = check_id(check)
        latest = store.latest_observation(cid)
        if latest is not None:
            overall_statuses.append(Status(latest["status"]))
        history = store.history(cid, limit=_HISTORY_FETCH_LIMIT)
        per_day = bucket_by_day(history, dates, tz)
        for i, day in enumerate(dates):
            status = per_day[day]
            if status is not None:
                day_buckets[i].append(status)
    overall = _worst_or_unknown(overall_statuses)
    days = [_worst_or_unknown(bucket) for bucket in day_buckets]
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
    table.add_column("overall", key="overall")
    for day in trailing_dates(today):
        table.add_column(day.strftime("%a"), key=day.isoformat())
    for row in rows:
        cells = [row.label, _status_cell(row.overall)]
        cells.extend(_status_cell(status) for status in row.days)
        table.add_row(*cells, key=row.key)
