"""Render run results as a copy-pasteable plain-text digest, or as JSON."""

from __future__ import annotations

import calendar
import json
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
)

from dbfresh.calendar import BusinessCalendar
from dbfresh.models import Result, RunResult, Status, split_value

if TYPE_CHECKING:
    from typing import TextIO


def format_timestamp(when: datetime, tz: tzinfo | None = None) -> str:
    """ISO 8601 at second precision, for consistent display everywhere.

    A naive ``when`` is assumed to already be UTC. The result is converted
    to ``tz`` (default UTC) and written with a trailing ``Z`` when that
    offset is zero, otherwise a numeric ``+HH:MM``/``-HH:MM`` offset. No
    microseconds.

    This default is a low-level formatting fallback for a direct call (e.g.
    a test exercising this function in isolation) -- it is deliberately
    independent of :func:`display_timezone`, the app's actual display-
    timezone *policy* (local by default), which every real caller (the CLI,
    the TUI) resolves first and passes in explicitly as ``tz``. That means
    this UTC default is never actually exercised in production; it only
    ever fires for a caller that skips :func:`display_timezone`.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    when = when.astimezone(tz if tz is not None else UTC).replace(
        microsecond=0
    )
    text = when.isoformat()
    return text[: -len("+00:00")] + "Z" if text.endswith("+00:00") else text


def format_timestamp_friendly(when: datetime, tz: tzinfo | None = None) -> str:
    """A human-scannable timestamp for the History view -- ISO date, 12-hour
    local time to the minute, and a weekday abbreviation, e.g.
    ``2026-07-17  2:12 PM (Tue)``. The hour is space-padded to two digits so
    a single-digit hour lines up under a two-digit one down a column.

    Unlike :func:`format_timestamp`'s ISO 8601 (kept for the digest and the
    JSON output, where an unambiguous machine- and copy-friendly form
    matters), this trades the numeric offset and seconds for easier
    scanning: the time is shown in ``tz`` (the app's display timezone,
    local by default) with no printed offset.

    The weekday and AM/PM come from the standard library
    (:data:`calendar.day_abbr` and ``strftime('%p')``) rather than
    hardcoded literals. dbfresh never calls :func:`locale.setlocale`, so
    both resolve in the process's default "C" locale -- English, and
    independent of the ``LC_*`` environment -- which keeps the output
    stable across machines and in tests; they localize only if the app
    ever opts in via ``setlocale``.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    when = when.astimezone(tz if tz is not None else UTC)
    hour12 = when.hour % 12 or 12
    weekday = calendar.day_abbr[when.weekday()]
    return f"{when:%Y-%m-%d} {hour12:>2}:{when:%M} {when:%p} ({weekday})"


def display_timezone(calendar: BusinessCalendar | None) -> tzinfo:
    """The app's actual display-timezone policy: a configured calendar's
    zone, else the local system timezone -- never UTC as a bare default.

    This is the one place that policy is decided; every real caller (the
    CLI, the TUI) calls this first and passes the result into
    :func:`format_timestamp` (via ``render_digest``/``render_history``) as
    an explicit ``tz``, which is why that function's own separate UTC
    default is never reached outside of a direct/test call -- the two
    defaults look inconsistent side by side, but they are intentionally
    different layers, not a bug.

    Local, not UTC, so the report header, history rows, and freshness's
    reconstructed last-update time are all local by default and consistent
    with each other -- still ISO 8601 throughout, since format_timestamp
    renders a numeric offset for any non-UTC zone, not just a trailing Z.
    A machine already running in UTC (most servers/CI) sees no change.
    """
    if calendar is not None:
        return calendar.zone
    local = datetime.now().astimezone().tzinfo
    assert (
        local is not None
    )  # astimezone() on an aware datetime always sets it
    return local


_DURATION_UNITS = (("d", 86400), ("h", 3600), ("m", 60), ("s", 1))


def _format_duration(seconds: float) -> str:
    """A human duration using its two most significant units, e.g. '5d 9h'."""
    remaining = int(abs(seconds))
    parts: list[str] = []
    for suffix, unit_seconds in _DURATION_UNITS:
        if remaining >= unit_seconds:
            count, remaining = divmod(remaining, unit_seconds)
            parts.append(f"{count}{suffix}")
            if len(parts) == 2:
                break
    return " ".join(parts) if parts else "0s"


_INTEGER_METRICS = frozenset({"duplicate_count", "row_count"})
_ROUNDED_METRICS = frozenset({"sum", "avg", "min", "max"})


def _format_observed(metric: str | None, value: Any) -> str:
    """A metric-aware human display of an observed scalar.

    Freshness is handled separately (:func:`_format_freshness_observed`) --
    its value is a lag in seconds needing a duration, not a plain number.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return str(value)
    if metric in _INTEGER_METRICS:
        return str(int(round(value)))
    if metric == "null_rate":
        return f"{value * 100:.1f}%"
    if metric in _ROUNDED_METRICS:
        return f"{value:.2f}"
    return str(value)


def _format_freshness_observed(
    lag_seconds: float,
    reference: datetime | None,
    tz: tzinfo | None,
    formatter: Callable[[datetime, tzinfo | None], str] = format_timestamp,
) -> str:
    """'<duration> stale', plus the reconstructed absolute last-update time
    when a reference instant is available.

    ``reference`` is the exact ``now`` that produced ``lag_seconds`` --
    ``run.started_at`` for a live digest -- so ``reference - lag`` recovers
    the source's last-update timestamp exactly, not approximately: runner.py
    resolves ``now`` once per run and reuses that same value both for the
    lag computation and (elsewhere) as the persisted ``observed_at``.

    ``formatter`` renders that absolute time -- the ISO
    :func:`format_timestamp` by default (the digest), or
    :func:`format_timestamp_friendly` when the History view passes it.
    """
    duration = _format_duration(lag_seconds)
    if reference is None:
        return f"{duration} stale"
    last_update = reference - timedelta(seconds=lag_seconds)
    return f"{duration} stale (last update: {formatter(last_update, tz)})"


def render_digest(
    run: RunResult, now: datetime | None = None, tz: tzinfo | None = None
) -> str:
    """A plain-text digest: a header with counts, then one block per non-OK check."""
    when = now if now is not None else datetime.now(UTC)
    counts = dict.fromkeys(Status, 0)
    for result in run.results:
        counts[result.status] += 1

    lines = [
        f"DATA CHECK REPORT — {format_timestamp(when, tz)}",
        f"{len(run.results)} checks · {counts[Status.OK]} passed"
        f" · {counts[Status.FAIL]} failed · {counts[Status.WARN]} warned"
        f" · {counts[Status.SKIPPED]} skipped · {counts[Status.ERROR]} unreachable",
    ]

    reference = run.started_at or when
    for result in run.results:
        if result.status in (Status.OK, Status.SKIPPED):
            continue
        obj = (
            f"{result.source}.{result.object}"
            if result.source
            else result.object
        )
        label = result.label or result.metric or "assert"
        header = f"✗ {obj} · {label}"
        if result.expected:
            header += f" — expected {result.expected}"
        lines.append("")
        lines.append(header)
        if result.error:
            lines.append(f"    {result.error}")
        elif result.diff:
            lines.append("    schema drift:")
            for change in result.diff:
                lines.append(f"      {change}")
        elif result.samples is not None:
            lines.append(f"    {result.value} row(s) violate the constraint")
            for row in result.samples[:10]:
                cells = "  ".join(
                    f"{key}={value}" for key, value in row.items()
                )
                lines.append(f"      {cells}")
        elif result.metric == "freshness" and isinstance(
            result.value, (int, float)
        ):
            observed = _format_freshness_observed(result.value, reference, tz)
            lines.append(f"    observed: {observed}")
        else:
            observed = _format_observed(result.metric, result.value)
            lines.append(f"    observed: {observed}")

    return "\n".join(lines)


def reconstruct_run(run: dict, observations: list[dict]) -> RunResult:
    """Rebuild a :class:`~dbfresh.models.RunResult` from a store run row and
    that run's observation rows (:meth:`~dbfresh.store.Store.latest_run`,
    :meth:`~dbfresh.store.Store.observations_for_run`).

    Lets a caller redraw a completed run's :func:`render_digest` output from
    persisted data alone -- e.g. the TUI's Report screen after a restart,
    when no in-session ``RunResult`` survived it. The observation table only
    ever persists a scalar/fingerprint per check plus its ``expected`` and
    ``error`` text, never the violating rows or schema diff a live run
    collects, so every reconstructed ``Result`` has ``samples=None`` and
    ``diff=None`` -- ``render_digest`` already falls back to its "observed:
    <value>" line whenever both are absent, so the reconstruction renders
    without either, rather than raising or faking them.
    """
    results = [
        Result(
            source=obs["source"],
            object=obs["object"],
            metric=obs["metric"],
            label=obs["label"],
            status=Status(obs["status"]),
            value=obs["value"]
            if obs["value"] is not None
            else obs["value_text"],
            expected=obs["expected"],
            error=obs["error"],
            check_id=obs["check_id"],
        )
        for obs in observations
    ]
    finished_at = run["finished_at"]
    return RunResult(
        results=results,
        status=Status(run["status"]),
        run_id=run["run_id"],
        started_at=datetime.fromisoformat(run["started_at"]),
        finished_at=datetime.fromisoformat(finished_at)
        if finished_at
        else None,
    )


def render_json(run: RunResult) -> str:
    """Machine-readable output: a stable envelope over every result.

    ``{status, run_id, started_at, finished_at, counts, results}`` -- this
    shape is a stable contract for downstream consumers, not an
    implementation detail free to drift between releases.
    """
    counts = dict.fromkeys(Status, 0)
    for result in run.results:
        counts[result.status] += 1
    payload = {
        "status": run.status.value,
        "run_id": run.run_id,
        "started_at": _optional_timestamp(run.started_at),
        "finished_at": _optional_timestamp(run.finished_at),
        "counts": {status.value: counts[status] for status in Status},
        "results": [_result_dict(result) for result in run.results],
    }
    return json.dumps(payload, default=str)


def _optional_timestamp(when: datetime | None) -> str | None:
    return format_timestamp(when) if when is not None else None


def render_candidates(object_: str, candidates: list[dict]) -> str:
    """List ambiguous check_id matches for ``dbfresh history OBJECT``."""
    lines = [
        f"multiple checks match {object_!r} — narrow with --source/--metric:"
    ]
    for c in candidates:
        label = c["metric"] or c["label"]
        lines.append(
            f"  {c['source']}.{c['object']} · {label} ({c['check_id']})"
        )
    return "\n".join(lines)


_HISTORY_EXPECTED_WIDTH = 24


def _summarize_fingerprint(fingerprint: str) -> str:
    """A schema fingerprint (``name:type|name:type|...``) shown as a column
    count. The full column list would dwarf every other value in the table,
    and the run digest's schema diff already carries that detail; here only
    the shape matters. An empty fingerprint (nothing reflected) reads as
    ``0 cols``."""
    n = fingerprint.count("|") + 1 if fingerprint else 0
    return f"{n} col{'s' if n != 1 else ''}"


def render_history(
    candidate: dict, rows: list[dict], tz: tzinfo | None = None
) -> str:
    """A check's recent values, expectations, and statuses.

    ``check_id`` rides along in the header line as a parenthetical -- still
    present for anyone who needs to copy it (e.g. into ``--metric``-less
    disambiguation elsewhere), but not its own leading line, which gave the
    internal hash more visual weight than the source.object.label it
    identifies.

    An ``expected`` column shows what each observation was compared
    against; a row with an ``error`` (an ERROR observation -- source
    unreachable, query failed) appends that text after the fixed-width
    columns rather than truncating it to fit one, since it is the row's
    most important content when present.
    """
    header = (
        f"{candidate['source']}.{candidate['object']} · {candidate['label']}"
    )
    lines = [f"CHECK HISTORY — {header} ({candidate['check_id']})"]
    if not rows:
        lines.append("")
        lines.append("no observations recorded")
        return "\n".join(lines)

    metric = candidate.get("metric")
    lines.append("")
    # Build each row's displayed value first so the value column can size to
    # its actual content. A single check's history is all one metric, so one
    # width stays coherent: a freshness row carries a reconstructed timestamp
    # and wants a wide column, while a number or a "N cols" schema summary
    # wants a narrow one -- padding every metric to the widest (freshness)
    # would waste most of the line for the rest.
    prepared: list[tuple[str, str, str, str, str | None]] = []
    for row in rows:
        value = row["value"] if row["value"] is not None else row["value_text"]
        observed_at = row["observed_at"]
        observed = format_timestamp_friendly(
            datetime.fromisoformat(observed_at), tz
        )
        if metric == "freshness" and isinstance(value, (int, float)):
            # The reconstructed absolute last-update time the digest also
            # shows (_format_freshness_observed): this row's own observed_at
            # is the "now" that produced this lag, the role run.started_at
            # plays there. The History view renders it in the same friendly
            # form as the observed_at column, not the digest's ISO.
            reference = datetime.fromisoformat(observed_at)
            display = _format_freshness_observed(
                value, reference, tz, format_timestamp_friendly
            )
        elif metric == "schema" and isinstance(value, str) and value:
            display = _summarize_fingerprint(value)
        else:
            display = _format_observed(metric, value)
        prepared.append(
            (
                observed,
                row["status"],
                display,
                row.get("expected") or "",
                row.get("error"),
            )
        )
    value_width = max([len("value")] + [len(p[2]) for p in prepared])
    lines.append(
        f"{'observed_at':<28} {'status':<8} {'value':<{value_width}} "
        f"{'expected':<{_HISTORY_EXPECTED_WIDTH}}"
    )
    for observed, status, display, expected, error in prepared:
        line = (
            f"{observed:<28} {status:<8} {display:<{value_width}} "
            f"{expected:<{_HISTORY_EXPECTED_WIDTH}}"
        )
        if error:
            # Collapse whitespace: an error can be multi-line (a driver's
            # traceback), and both this table and the TUI History screen map
            # one line per observation, so the row stays on a single line.
            line += f"  — {' '.join(str(error).split())}"
        lines.append(line)
    return "\n".join(lines)


def _result_dict(result: Result) -> dict:
    value, value_text = split_value(result.value)
    observed = None if result.value is None else str(result.value)
    return {
        "check_id": result.check_id,
        "source": result.source,
        "object": result.object,
        "metric": result.metric,
        "label": result.label,
        "tier": result.tier,
        "status": result.status.value,
        "value": value,
        "value_text": value_text,
        "expected": result.expected,
        "observed": observed,
        "error": result.error,
        "samples": result.samples,
        "diff": result.diff,
    }


def show_progress(
    json_output: bool, no_progress: bool, stream: TextIO | None = None
) -> bool:
    """Whether a live progress bar should render for this run.

    Suppressed by ``--json`` (a machine consumer has no use for it and it
    would corrupt the output stream), by ``--no-progress``, and whenever
    ``stream`` (stdout by default) is not a terminal -- piped or redirected
    output, or output captured by a test.
    """
    if json_output or no_progress:
        return False
    stream = stream if stream is not None else sys.stdout
    isatty = getattr(stream, "isatty", None)
    return bool(isatty()) if isatty is not None else False


@contextmanager
def progress_reporter(
    total: int, enabled: bool, console: Console | None = None
) -> Iterator[Callable[[Result], None]]:
    """A context manager yielding an ``on_result`` callback for a run.

    When ``enabled``, renders a live M-of-N bar (via rich) that advances
    once per completed check; yields a no-op callback otherwise, so the
    caller never has to branch on whether progress is shown. Checks across
    sources complete concurrently on separate threads, so the advance is
    guarded by a lock.
    """
    if not enabled:
        yield lambda _result: None
        return

    lock = threading.Lock()
    columns = (
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
    )
    with Progress(*columns, console=console) as progress:
        task = progress.add_task("running checks", total=total)

        def _advance(_result: Result) -> None:
            with lock:
                progress.advance(task)

        yield _advance
