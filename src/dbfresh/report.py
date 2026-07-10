"""Render run results as a copy-pasteable plain-text digest."""

from __future__ import annotations

import json
from datetime import datetime

from dbfresh.engine import Result, RunResult, Status


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
        label = result.label or result.metric or "assert"
        lines.append("")
        lines.append(f"✗ {obj} · {label}")
        if result.error:
            lines.append(f"    {result.error}")
        elif result.samples is not None:
            lines.append(f"    {result.value} row(s) violate the constraint")
            for row in result.samples[:10]:
                cells = "  ".join(f"{key}={value}" for key, value in row.items())
                lines.append(f"      {cells}")
        else:
            lines.append(f"    expected {result.expected}   observed {result.value}")

    return "\n".join(lines)


def render_json(run: RunResult) -> str:
    """Machine-readable output: the worst status and every result."""
    payload = {
        "status": run.status.value,
        "results": [_result_dict(result) for result in run.results],
    }
    return json.dumps(payload, default=str)


def _result_dict(result: Result) -> dict:
    return {
        "check_id": result.check_id,
        "source": result.source,
        "object": result.object,
        "metric": result.metric,
        "label": result.label,
        "status": result.status.value,
        "value": result.value,
        "expected": result.expected,
        "error": result.error,
        "samples": result.samples,
    }
