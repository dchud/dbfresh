"""Run checks, evaluate results, and aggregate status."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
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
    source: str = ""
    value: Any = None
    expected: str | None = None
    error: str | None = None
    label: str | None = None
    samples: list | None = None


def evaluate_check(check: Check, adapter: Any, now: datetime | None = None) -> Result:
    """Compile, execute, and evaluate one check against an adapter.

    Any connection or query failure maps to ``ERROR`` — never a silent pass.
    """
    now = now or datetime.now(UTC)
    if check.assert_ is not None:
        return _evaluate_assertion(check, adapter)
    if check.metric == "freshness":
        return _evaluate_freshness(check, adapter, now)
    sql = compile_metric_sql(check, adapter.dialect)
    expected = check.expect.describe() if check.expect else None
    try:
        value = adapter.scalar(sql)
    except Exception as exc:  # unreachable source / query error -> ERROR
        return Result(
            object=check.object,
            metric=check.metric,
            status=Status.ERROR,
            source=check.source,
            expected=expected,
            error=str(exc),
        )
    if value is None:
        return _empty_result(check, expected)
    passed = check.expect.evaluate(value) if check.expect else True
    if passed:
        status = Status.OK
    else:
        status = Status.WARN if check.severity == "warn" else Status.FAIL
    return Result(
        object=check.object,
        metric=check.metric,
        status=status,
        source=check.source,
        value=value,
        expected=expected,
    )


def _empty_result(check: Check, expected: str | None) -> Result:
    """Handle a null scalar (empty table / MAX of no rows)."""
    if check.metric == "null_rate":
        return Result(
            object=check.object,
            metric=check.metric,
            status=Status.ERROR,
            source=check.source,
            expected=expected,
            error="empty result: cannot compute null_rate",
        )
    status = Status.OK if check.allow_empty else Status.FAIL
    if not check.allow_empty and check.severity == "warn":
        status = Status.WARN
    return Result(
        object=check.object,
        metric=check.metric,
        status=status,
        source=check.source,
        expected=expected,
    )


def _evaluate_assertion(check: Check, adapter: Any) -> Result:
    """Run an assert predicate; any row for which it is false is a violation."""
    violation = f"FROM {check.object} WHERE NOT ({check.assert_})"
    label = f"assert {check.assert_}"
    try:
        count = adapter.scalar(f"SELECT COUNT(*) {violation}")
    except Exception as exc:  # unreachable source / query error -> ERROR
        return Result(
            object=check.object,
            metric=None,
            status=Status.ERROR,
            source=check.source,
            label=label,
            error=str(exc),
        )
    if count == 0:
        return Result(
            object=check.object,
            metric=None,
            status=Status.OK,
            source=check.source,
            value=0,
            label=label,
            samples=[],
        )
    samples = adapter.rows(adapter.dialect.limit(f"SELECT * {violation}", 20))
    status = Status.WARN if check.severity == "warn" else Status.FAIL
    return Result(
        object=check.object,
        metric=None,
        status=status,
        source=check.source,
        value=count,
        label=label,
        samples=samples,
    )


def _evaluate_freshness(check: Check, adapter: Any, now: datetime) -> Result:
    """Compute freshness lag (now - MAX(column)) and evaluate it against max_lag."""
    sql = compile_metric_sql(check, adapter.dialect)
    expected = check.expect.describe() if check.expect else None
    try:
        raw = adapter.scalar(sql)
    except Exception as exc:  # unreachable source / query error -> ERROR
        return Result(
            object=check.object,
            metric=check.metric,
            status=Status.ERROR,
            source=check.source,
            expected=expected,
            error=str(exc),
        )
    if raw is None:
        return _empty_result(check, expected)
    lag_seconds = (now - _to_aware_utc(raw)).total_seconds()
    passed = check.expect.evaluate(lag_seconds) if check.expect else True
    if passed:
        status = Status.OK
    else:
        status = Status.WARN if check.severity == "warn" else Status.FAIL
    return Result(
        object=check.object,
        metric=check.metric,
        status=status,
        source=check.source,
        value=lag_seconds,
        expected=expected,
    )


def _to_aware_utc(value: Any) -> datetime:
    """Coerce a DB timestamp to a tz-aware UTC datetime; naive is assumed UTC."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


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
    now = datetime.now(UTC)
    results = [evaluate_check(check, adapters[check.source], now) for check in checks]
    return RunResult(results=results, status=worst_status(r.status for r in results))
