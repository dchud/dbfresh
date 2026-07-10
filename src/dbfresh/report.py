"""Render run results as a copy-pasteable plain-text digest."""

from __future__ import annotations

from datetime import datetime

from dbfresh.engine import RunResult, Status


def render_digest(run: RunResult, now: datetime | None = None) -> str:
    """A plain-text digest: a header with counts, then one block per non-OK check."""
    when = now if now is not None else datetime.now().astimezone()
    counts = dict.fromkeys(Status, 0)
    for result in run.results:
        counts[result.status] += 1

    lines = [
        f"DATA CHECK REPORT — {when:%Y-%m-%d %H:%M %Z}",
        f"{len(run.results)} checks · {counts[Status.OK]} passed"
        f" · {counts[Status.FAIL]} failed · {counts[Status.WARN]} warned"
        f" · {counts[Status.SKIPPED]} skipped · {counts[Status.ERROR]} unreachable",
    ]

    for result in run.results:
        if result.status in (Status.OK, Status.SKIPPED):
            continue
        obj = f"{result.source}.{result.object}" if result.source else result.object
        lines.append("")
        lines.append(f"✗ {obj} · {result.metric or 'assert'}")
        if result.error:
            lines.append(f"    {result.error}")
        else:
            lines.append(f"    expected {result.expected}   observed {result.value}")

    return "\n".join(lines)
