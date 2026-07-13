"""Render run results as a copy-pasteable plain-text digest, or as JSON."""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, tzinfo
from typing import TYPE_CHECKING

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
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    when = when.astimezone(tz if tz is not None else UTC).replace(microsecond=0)
    text = when.isoformat()
    return text[: -len("+00:00")] + "Z" if text.endswith("+00:00") else text


def display_timezone(calendar: BusinessCalendar | None) -> tzinfo | None:
    """The report display timezone: a configured calendar's zone, else UTC."""
    return calendar.zone if calendar is not None else None


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

    for result in run.results:
        if result.status in (Status.OK, Status.SKIPPED):
            continue
        obj = f"{result.source}.{result.object}" if result.source else result.object
        label = result.label or result.metric or "assert"
        lines.append("")
        lines.append(f"✗ {obj} · {label}")
        if result.error:
            lines.append(f"    {result.error}")
        elif result.diff:
            lines.append(f"    schema drift (expected {result.expected})")
            for change in result.diff:
                lines.append(f"      {change}")
        elif result.samples is not None:
            lines.append(f"    {result.value} row(s) violate the constraint")
            for row in result.samples[:10]:
                cells = "  ".join(f"{key}={value}" for key, value in row.items())
                lines.append(f"      {cells}")
        else:
            lines.append(f"    expected {result.expected}   observed {result.value}")

    return "\n".join(lines)


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
    lines = [f"multiple checks match {object_!r} — narrow with --source/--metric:"]
    for c in candidates:
        label = c["metric"] or c["label"]
        lines.append(f"  {c['source']}.{c['object']} · {label} ({c['check_id']})")
    return "\n".join(lines)


def render_history(candidate: dict, rows: list[dict], tz: tzinfo | None = None) -> str:
    """A check's recent values, statuses, and a simple up/down trend."""
    header = f"{candidate['source']}.{candidate['object']} · {candidate['label']}"
    lines = [f"CHECK HISTORY — {header}", f"check_id {candidate['check_id']}"]
    if not rows:
        lines.append("")
        lines.append("no observations recorded")
        return "\n".join(lines)

    lines.append("")
    lines.append(f"{'observed_at':<28} {'status':<8} {'value':<16} trend")
    previous: float | None = None
    for row in rows:
        value = row["value"] if row["value"] is not None else row["value_text"]
        trend = ""
        if isinstance(value, (int, float)) and isinstance(previous, (int, float)):
            if value > previous:
                trend = "▲"
            elif value < previous:
                trend = "▼"
            else:
                trend = "="
        observed = format_timestamp(datetime.fromisoformat(row["observed_at"]), tz)
        lines.append(f"{observed:<28} {row['status']:<8} {str(value):<16} {trend}")
        if isinstance(value, (int, float)):
            previous = value
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
