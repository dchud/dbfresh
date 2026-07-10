"""Run checks, evaluate results, and aggregate status."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from dbfresh.checks import Check, compile_metric_sql


class Status(StrEnum):
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"


@dataclass
class Result:
    object: str
    metric: str | None
    status: Status
    value: Any = None
    expected: str | None = None
    error: str | None = None


def evaluate_check(check: Check, adapter: Any) -> Result:
    """Compile, execute, and evaluate one check against an adapter.

    Any connection or query failure maps to ``ERROR`` — never a silent pass.
    """
    sql = compile_metric_sql(check)
    expected = check.expect.describe() if check.expect else None
    try:
        value = adapter.scalar(sql)
    except Exception as exc:  # unreachable source / query error -> ERROR
        return Result(
            object=check.object,
            metric=check.metric,
            status=Status.ERROR,
            expected=expected,
            error=str(exc),
        )
    passed = check.expect.evaluate(value) if check.expect else True
    if passed:
        status = Status.OK
    else:
        status = Status.WARN if check.severity == "warn" else Status.FAIL
    return Result(
        object=check.object,
        metric=check.metric,
        status=status,
        value=value,
        expected=expected,
    )


_SEVERITY = {
    Status.OK: 0,
    Status.SKIPPED: 0,
    Status.WARN: 1,
    Status.FAIL: 2,
    Status.ERROR: 3,
}
_RANK_STATUS = {0: Status.OK, 1: Status.WARN, 2: Status.FAIL, 3: Status.ERROR}


def worst_status(statuses: Iterable[Status]) -> Status:
    """The most severe status; OK when empty. ERROR (3) outranks FAIL (2)."""
    rank = max((_SEVERITY[s] for s in statuses), default=0)
    return _RANK_STATUS[rank]


def exit_code(status: Status) -> int:
    """Map a status to a process exit code: OK/SKIPPED 0, WARN 1, FAIL 2, ERROR 3."""
    return _SEVERITY[status]


@dataclass
class RunResult:
    results: list[Result]
    status: Status


def run_checks(adapters: dict[str, Any], checks: list[Check]) -> RunResult:
    """Evaluate every check against its source's adapter and aggregate status."""
    results = [evaluate_check(check, adapters[check.source]) for check in checks]
    return RunResult(results=results, status=worst_status(r.status for r in results))
