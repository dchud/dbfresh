"""Run checks, evaluate results, and aggregate status."""

from __future__ import annotations

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
