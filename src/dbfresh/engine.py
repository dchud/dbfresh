"""Run checks, evaluate results, and aggregate status."""

from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from dbfresh.adapters.databricks import validate_freshness_source
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
    if expect is not None and expect.operator == "vs_previous":
        return _evaluate_vs_previous(check, adapter, now, expect, calendar, store)
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


def _freshness_raw(check: Check, adapter: Any) -> Any:
    """The observed freshness timestamp, dispatched on ``check.freshness_source``.

    ``column`` runs the usual ``MAX(column)`` query. The two DESCRIBE origins
    are metadata-only (no column) and table-only: ``describe()`` supplies the
    object's ``is_view`` flag, validated here against the origin and the
    dialect's declared capability (a view must use ``column`` instead) --
    static capability is already enforced at config-validation time, but
    ``is_view`` is only knowable from a live ``describe()``, so it is
    (re-)checked here. ``describe_detail`` reuses that same ``describe()``
    call for its timestamp; ``describe_history`` reads it separately.
    """
    if check.freshness_source == "column":
        sql = compile_metric_sql(check, adapter.dialect)
        return adapter.scalar(sql)
    info = adapter.describe(check.object)
    validate_freshness_source(check.freshness_source, adapter.dialect, info.is_view)
    if check.freshness_source == "describe_detail":
        return info.last_modified
    return adapter.describe_history_last_modified(check.object)


def _evaluate_freshness(
    check: Check,
    adapter: Any,
    now: datetime,
    expect: Expectation | None,
    calendar: BusinessCalendar | None,
) -> Result:
    """Compute freshness lag (now - observed timestamp) and evaluate max_lag.

    The observed timestamp's origin is ``check.freshness_source``; everything
    from there on (business-calendar lag, expectation) is the same regardless
    of where the timestamp came from.
    """
    expected = expect.describe() if expect else None
    try:
        raw = _freshness_raw(check, adapter)
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
    if not passed and prior is not None:
        result.diff = diff_fingerprints(fingerprint, prior)
    return result


_ON_MISSING_STATUS = {
    "pass": Status.OK,
    "warn": Status.WARN,
    "skip": Status.SKIPPED,
}


def _evaluate_vs_previous(
    check: Check,
    adapter: Any,
    now: datetime,
    expect: Expectation,
    calendar: BusinessCalendar | None,
    store: Any | None,
) -> Result:
    """Compare the current scalar to a prior observation (§8.3).

    The current value comes from the same SQL path as any other numeric
    metric. The baseline is read from ``store`` per ``expect.operand``:
    ``baseline: previous`` is the most recent prior observation excluding
    ERROR/SKIPPED; ``baseline: last_same_weekday`` is the most recent prior
    observation on today's calendar-tz weekday, at least 6 calendar days
    back. No store, or no matching baseline, evaluates per ``on_missing``;
    a zero baseline falls back to delta guards when configured, else is
    also treated as missing.
    """
    sql = compile_metric_sql(check, adapter.dialect)
    expected = expect.describe()
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

    spec = expect.operand
    baseline = _read_vs_previous_baseline(check, now, calendar, store, spec)
    has_delta = spec["min_delta"] is not None or spec["max_delta"] is not None
    if baseline is not None and baseline == 0 and not has_delta:
        baseline = None  # zero baseline with no delta fallback -> on_missing

    if baseline is None:
        status = _ON_MISSING_STATUS[spec["on_missing"]]
        return Result(
            object=check.object,
            metric=check.metric,
            status=status,
            source=check.source,
            value=value,
            expected=expected,
        )

    passed = _vs_previous_passed(value, baseline, spec)
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


def _read_vs_previous_baseline(
    check: Check,
    now: datetime,
    calendar: BusinessCalendar | None,
    store: Any | None,
    spec: dict,
) -> float | None:
    """The baseline scalar for ``vs_previous``, or ``None`` if unavailable."""
    if store is None:
        return None
    cid = check_id(check)
    if spec["baseline"] == "previous":
        observation = store.latest_clean_observation(cid)
    else:  # last_same_weekday
        run_date = calendar.local_date(now) if calendar is not None else now.date()
        observation = store.last_same_weekday_observation(cid, run_date)
    return observation.get("value") if observation is not None else None


def _vs_previous_passed(value: float, baseline: float, spec: dict) -> bool:
    """Ratio guards (when ``baseline`` != 0) and/or delta guards; every
    configured guard must pass (§8.3)."""
    min_ratio, max_ratio = spec["min_ratio"], spec["max_ratio"]
    min_delta, max_delta = spec["min_delta"], spec["max_delta"]
    passed = True
    if baseline != 0 and (min_ratio is not None or max_ratio is not None):
        ratio = value / baseline
        if min_ratio is not None:
            passed = passed and ratio >= min_ratio
        if max_ratio is not None:
            passed = passed and ratio <= max_ratio
    if min_delta is not None or max_delta is not None:
        delta = value - baseline
        if min_delta is not None:
            passed = passed and delta >= min_delta
        if max_delta is not None:
            passed = passed and delta <= max_delta
    return passed


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
