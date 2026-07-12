"""Run checks, evaluate results, and aggregate status."""

from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from dbfresh.calendar import BusinessCalendar, weekday_key
from dbfresh.checks import (
    Check,
    Expectation,
    check_id,
    compile_metric_sql,
    diff_fingerprints,
    fingerprint_columns,
)


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
    check_id: str | None = None
    diff: list[str] | None = None


def evaluate_check(
    check: Check,
    adapter: Any,
    now: datetime | None = None,
    calendar: BusinessCalendar | None = None,
    store: Any | None = None,
) -> Result:
    """Compile, execute, and evaluate one check against an adapter.

    Wraps :func:`_evaluate_check` to stamp the stable ``check_id`` (§8.2) on
    every returned ``Result``, regardless of which branch produced it.

    ``store`` is an optional read-only handle onto prior observations (any
    object exposing ``latest_observation(check_id)``, e.g. a
    :class:`~dbfresh.store.Store`) used by history-based expectations —
    currently the schema check's ``unchanged``, and later ``vs_previous``
    (§8.3). Persistence of *this* run's results happens after the run
    completes, so the store holds only prior runs during evaluation.
    """
    result = _evaluate_check(check, adapter, now, calendar, store)
    result.check_id = check_id(check)
    return result


def _effective_expectation(
    check: Check, calendar: BusinessCalendar | None, now: datetime
) -> Expectation | None:
    """Select today's expectation: on_holiday -> by_weekday[today] -> expect."""
    if calendar is None:
        return check.expect
    run_date = calendar.local_date(now)
    if check.on_holiday is not None and calendar.is_holiday(run_date):
        return check.on_holiday
    if check.by_weekday:
        key = weekday_key(run_date)
        if key in check.by_weekday:
            return check.by_weekday[key]
    return check.expect


def _should_skip(
    check: Check, calendar: BusinessCalendar | None, now: datetime
) -> bool:
    """§7.4: skip a check when off-schedule and skip_off_schedule is set."""
    if not check.skip_off_schedule or calendar is None:
        return False
    run_date = calendar.local_date(now)
    return not calendar.is_business_day(run_date)


def _evaluate_check(
    check: Check,
    adapter: Any,
    now: datetime | None = None,
    calendar: BusinessCalendar | None = None,
    store: Any | None = None,
) -> Result:
    now = now or datetime.now(UTC)
    if _should_skip(check, calendar, now):
        label = f"assert {check.assert_}" if check.assert_ is not None else None
        return Result(
            object=check.object,
            metric=check.metric,
            status=Status.SKIPPED,
            source=check.source,
            label=label,
        )
    expect = _effective_expectation(check, calendar, now)
    if check.assert_ is not None:
        return _evaluate_assertion(check, adapter)
    if check.metric == "freshness":
        return _evaluate_freshness(check, adapter, now, expect, calendar)
    if check.metric == "schema":
        return _evaluate_schema(check, adapter, expect, store)
    sql = compile_metric_sql(check, adapter.dialect)
    expected = expect.describe() if expect else None
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
    passed = expect.evaluate(value) if expect else True
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


def _evaluate_freshness(
    check: Check,
    adapter: Any,
    now: datetime,
    expect: Expectation | None,
    calendar: BusinessCalendar | None,
) -> Result:
    """Compute freshness lag (now - MAX(column)) and evaluate it against max_lag."""
    sql = compile_metric_sql(check, adapter.dialect)
    expected = expect.describe() if expect else None
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
    t0 = _to_aware_utc(raw)
    if check.calendar == "business" and calendar is not None:
        lag_seconds = calendar.business_time_between(t0, now).total_seconds()
    else:
        lag_seconds = (now - t0).total_seconds()
    passed = expect.evaluate(lag_seconds) if expect else True
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


def _evaluate_schema(
    check: Check,
    adapter: Any,
    expect: Expectation | None,
    store: Any | None,
) -> Result:
    """Fingerprint the object's columns and evaluate unchanged/equals (§6.2).

    ``schema`` is table-level and never compiles to SQL: it calls
    ``adapter.describe(object)`` and reduces the columns to a fingerprint
    (:func:`~dbfresh.checks.fingerprint_columns`). ``unchanged`` compares
    against the most recent prior observation read from ``store`` (``None``
    when there is no prior observation or no store — first run passes and
    establishes the baseline); ``equals`` compares against a pinned
    fingerprint. On drift, ``diff`` carries the added/removed/retyped columns.
    """
    expected = expect.describe() if expect else None
    try:
        info = adapter.describe(check.object)
    except Exception as exc:  # unreachable source / missing object -> ERROR
        return Result(
            object=check.object,
            metric=check.metric,
            status=Status.ERROR,
            source=check.source,
            expected=expected,
            error=str(exc),
        )
    fingerprint = fingerprint_columns(info.columns)
    if expect is None:
        return Result(
            object=check.object,
            metric=check.metric,
            status=Status.OK,
            source=check.source,
            value=fingerprint,
        )

    prior: str | None
    if expect.operator == "unchanged":
        prior = None
        if store is not None:
            observation = store.latest_observation(check_id(check))
            if observation is not None:
                prior = observation.get("value_text")
        passed = prior is None or fingerprint == prior
    else:  # equals / eq: compare to the pinned fingerprint
        prior = expect.operand
        passed = expect.evaluate(fingerprint)

    if passed:
        status = Status.OK
    else:
        status = Status.WARN if check.severity == "warn" else Status.FAIL
    result = Result(
        object=check.object,
        metric=check.metric,
        status=status,
        source=check.source,
        value=fingerprint,
        expected=expected,
    )
    if not passed and prior:
        result.diff = diff_fingerprints(fingerprint, prior)
    return result


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


def run_checks(
    adapters: dict[str, Any],
    checks: list[Check],
    calendar: BusinessCalendar | None = None,
    now: datetime | None = None,
    store: Any | None = None,
) -> RunResult:
    """Evaluate checks per source and aggregate the worst status.

    Sources run in parallel, one worker thread each; a source's own checks run
    serially on its single connection, which is never shared across threads.

    ``store`` is threaded straight through to :func:`evaluate_check` for
    history-based expectations; omit it and every run is otherwise identical
    to a run with no store (schema ``unchanged`` always passes).
    """
    now = now or datetime.now(UTC)
    by_source: dict[str, list[Check]] = {}
    for check in checks:
        by_source.setdefault(check.source, []).append(check)

    def run_source(source_checks: list[Check]) -> list[Result]:
        return [
            evaluate_check(check, adapters[check.source], now, calendar, store)
            for check in source_checks
        ]

    results: list[Result] = []
    if by_source:
        with ThreadPoolExecutor(max_workers=len(by_source)) as pool:
            for source_results in pool.map(run_source, by_source.values()):
                results.extend(source_results)
    return RunResult(results=results, status=worst_status(r.status for r in results))
