"""Run checks, evaluate results, and aggregate status."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

import structlog

from dbfresh.adapters.base import (
    Adapter,
    HistoryAwareAdapter,
    validate_freshness_source,
)
from dbfresh.calendar import BusinessCalendar, weekday_key
from dbfresh.checks import (
    Check,
    Expectation,
    check_id,
    compile_metric_sql,
    diff_fingerprints,
    fingerprint_columns,
)
from dbfresh.models import (
    ObservationReader,
    Result,
    RunResult,
    Status,
    worst_status,
)
from dbfresh.models import exit_code as exit_code

log = structlog.get_logger(__name__)


def _result(check: Check, status: Status, **fields: Any) -> Result:
    """Build a Result for ``check``, defaulting object/metric/source from it.

    A keyword in ``fields`` overrides the matching default -- e.g. an
    assertion check's Results always pin ``metric=None`` regardless of
    ``check.metric`` -- and any other Result field (``value``, ``expected``,
    ``label``, ...) is passed straight through. ``tier`` is derived from the
    check, not declared: table when it names no column or key, column
    otherwise.
    """
    base: dict[str, Any] = {
        "object": check.object,
        "metric": check.metric,
        "source": check.source,
        "tier": "column" if (check.column or check.key) else "table",
    }
    base.update(fields)
    return Result(status=status, **base)


def _error_result(check: Check, exc: BaseException, **fields: Any) -> Result:
    """A ``Status.ERROR`` Result for ``check``, carrying ``exc``'s message."""
    return _result(check, Status.ERROR, error=str(exc), **fields)


def _verdict(check: Check, passed: bool) -> Status:
    """OK when ``passed``; else WARN if ``check.severity`` is warn, else FAIL."""
    if passed:
        return Status.OK
    return Status.WARN if check.severity == "warn" else Status.FAIL


def evaluate_check(
    check: Check,
    adapter: Adapter,
    now: datetime | None = None,
    calendar: BusinessCalendar | None = None,
    store: ObservationReader | None = None,
) -> Result:
    """Compile, execute, and evaluate one check against an adapter.

    Wraps :func:`_evaluate_check` to stamp the stable ``check_id`` on
    every returned ``Result``, regardless of which branch produced it, and
    to catch any exception that escapes it -- e.g. ``compile_metric_sql``
    raising on an unvalidated metric name, which happens outside every
    per-metric try/except below. This is the outer safety net: each check
    runs on a ``ThreadPoolExecutor`` worker (see ``run_checks``), and an
    uncaught exception there would abort that worker and discard every
    other source's completed results, not just this one check's.

    ``store`` is an optional read-only handle onto prior observations (any
    object exposing ``latest_observation(check_id)``, e.g. a
    :class:`~dbfresh.store.Store`) used by history-based expectations —
    currently the schema check's ``unchanged``, and later ``vs_previous``.
    Persistence of *this* run's results happens after the run
    completes, so the store holds only prior runs during evaluation.
    """
    try:
        result = _evaluate_check(check, adapter, now, calendar, store)
    except Exception as exc:
        result = _error_result(check, exc)
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
    """Skip a check when off-schedule and skip_off_schedule is set."""
    if not check.skip_off_schedule or calendar is None:
        return False
    run_date = calendar.local_date(now)
    return not calendar.is_business_day(run_date)


def _evaluate_check(
    check: Check,
    adapter: Adapter,
    now: datetime | None = None,
    calendar: BusinessCalendar | None = None,
    store: ObservationReader | None = None,
) -> Result:
    now = now or datetime.now(UTC)
    if _should_skip(check, calendar, now):
        return _result(check, Status.SKIPPED, label=_assertion_label(check))
    expect = _effective_expectation(check, calendar, now)
    if check.assert_ is not None:
        return _evaluate_assertion(check, adapter)
    if check.assert_sql is not None:
        return _evaluate_assert_sql(check, adapter)
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
        return _error_result(check, exc, expected=expected)
    if value is None:
        return _empty_result(check, expected)
    passed = expect.evaluate(value) if expect else True
    status = _verdict(check, passed)
    return _result(check, status, value=value, expected=expected)


def _empty_result(check: Check, expected: str | None) -> Result:
    """Handle a null scalar (empty table / MAX of no rows)."""
    if check.metric == "null_rate":
        return _result(
            check,
            Status.ERROR,
            expected=expected,
            error="empty result: cannot compute null_rate",
        )
    return _result(check, _verdict(check, check.allow_empty), expected=expected)


def _assertion_label(check: Check) -> str | None:
    """The digest label for an ``assert:``/``assert_sql:`` check, else ``None``."""
    if check.assert_ is not None:
        return f"assert {check.assert_}"
    if check.assert_sql is not None:
        return f"assert_sql {check.assert_sql}"
    return None


def _evaluate_assertion(check: Check, adapter: Adapter) -> Result:
    """Run an assert predicate; any row for which it is false is a violation."""
    violation = f"FROM {check.object} WHERE NOT ({check.assert_})"
    label = _assertion_label(check)
    try:
        count = adapter.scalar(f"SELECT COUNT(*) {violation}")
    except Exception as exc:  # unreachable source / query error -> ERROR
        return _error_result(check, exc, metric=None, label=label)
    if count == 0:
        return _result(check, Status.OK, metric=None, value=0, label=label, samples=[])
    samples = adapter.rows(adapter.dialect.limit(f"SELECT * {violation}", 20))
    status = _verdict(check, False)
    return _result(
        check, status, metric=None, value=count, label=label, samples=samples
    )


_ASSERT_SQL_CAP = 20


def _evaluate_assert_sql(check: Check, adapter: Adapter) -> Result:
    """Run a raw, author-supplied violation-selecting query, unmodified.

    Unlike ``assert:`` (a predicate compiled into a ``COUNT(*)`` query plus
    a separately capped evidence query), ``assert_sql:`` is arbitrary SQL
    the author wrote themselves, selecting the violating rows. It is never
    rewritten to inject a row cap -- that corrupts author SQL (a cap
    injected inside a CTE truncates the scan instead of the returned rows;
    ``SELECT DISTINCT`` becomes invalid syntax under some dialects'
    rewrite) -- so it runs exactly as authored, capped only at fetch time
    via ``rows_limited``, which fetches at most ``CAP + 1`` rows off the
    cursor. Below the cap, the fetched length is the exact violation
    count; a fetch of ``CAP + 1`` means the true count is ``CAP`` or more
    but is not itself known, so the persisted/displayed value reads
    ``"CAP+"`` rather than the literal (and meaningless) ``CAP + 1``.
    """
    assert check.assert_sql is not None  # only called from that branch
    label = _assertion_label(check)
    try:
        rows = adapter.rows_limited(check.assert_sql, _ASSERT_SQL_CAP + 1)
    except Exception as exc:  # unreachable source / query error -> ERROR
        return _error_result(check, exc, metric=None, label=label)
    count = len(rows)
    if count == 0:
        return _result(check, Status.OK, metric=None, value=0, label=label, samples=[])
    capped = count > _ASSERT_SQL_CAP
    value: int | str = f"{_ASSERT_SQL_CAP}+" if capped else count
    status = _verdict(check, False)
    return _result(
        check, status, metric=None, value=value, label=label, samples=rows[:10]
    )


def _freshness_raw(check: Check, adapter: Adapter) -> Any:
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
    # describe_history is a Databricks-only capability, not part of the
    # base Adapter contract; validate_freshness_source above has already
    # confirmed the dialect declares it, so only an adapter implementing
    # HistoryAwareAdapter can reach this line at runtime.
    return cast(HistoryAwareAdapter, adapter).describe_history_last_modified(
        check.object
    )


def _evaluate_freshness(
    check: Check,
    adapter: Adapter,
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
        return _error_result(check, exc, expected=expected)
    if raw is None:
        return _empty_result(check, expected)
    t0 = _to_aware_utc(raw, check.source_timezone)
    if check.calendar == "business" and calendar is not None:
        lag_seconds = calendar.business_time_between(t0, now).total_seconds()
    else:
        lag_seconds = (now - t0).total_seconds()
    passed = expect.evaluate(lag_seconds) if expect else True
    status = _verdict(check, passed)
    return _result(check, status, value=lag_seconds, expected=expected)


def _evaluate_schema(
    check: Check,
    adapter: Adapter,
    expect: Expectation | None,
    store: ObservationReader | None,
) -> Result:
    """Fingerprint the object's columns and evaluate unchanged/equals.

    ``schema`` is table-level and never compiles to SQL: it calls
    ``adapter.describe(object)`` and reduces the columns to a fingerprint
    (:func:`~dbfresh.checks.fingerprint_columns`). ``unchanged`` compares
    against the most recent prior observation that actually recorded a
    fingerprint (``store.latest_fingerprint_observation`` -- ``None`` when
    there is no such observation or no store — first run passes and
    establishes the baseline). A SKIPPED or ERROR observation carries no
    fingerprint and is skipped past rather than read as "no baseline": once
    a drift is detected it alarms (FAIL/WARN) exactly once, and the new
    shape becomes the baseline for the next run -- pin a fingerprint with
    ``equals`` instead of ``unchanged`` for an alarm that never
    self-clears. ``equals`` compares against a pinned fingerprint. On
    drift, ``diff`` carries the added/removed/retyped columns.
    """
    expected = expect.describe() if expect else None
    try:
        info = adapter.describe(check.object)
    except Exception as exc:  # unreachable source / missing object -> ERROR
        return _error_result(check, exc, expected=expected)
    fingerprint = fingerprint_columns(info.columns)
    if expect is None:
        return _result(check, Status.OK, value=fingerprint)

    prior: str | None
    if expect.operator == "unchanged":
        prior = None
        if store is not None:
            observation = store.latest_fingerprint_observation(check_id(check))
            if observation is not None:
                prior = observation.get("value_text")
        passed = prior is None or fingerprint == prior
    else:  # equals / eq: compare to the pinned fingerprint
        prior = expect.operand
        passed = expect.evaluate(fingerprint)

    status = _verdict(check, passed)
    result = _result(check, status, value=fingerprint, expected=expected)
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
    adapter: Adapter,
    now: datetime,
    expect: Expectation,
    calendar: BusinessCalendar | None,
    store: ObservationReader | None,
) -> Result:
    """Compare the current scalar to a prior observation.

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
        return _error_result(check, exc, expected=expected)
    if value is None:
        return _empty_result(check, expected)

    spec = expect.operand
    baseline = _read_vs_previous_baseline(check, now, calendar, store, spec)
    has_delta = spec["min_delta"] is not None or spec["max_delta"] is not None
    if baseline is not None and baseline == 0 and not has_delta:
        baseline = None  # zero baseline with no delta fallback -> on_missing

    if baseline is None:
        status = _ON_MISSING_STATUS[spec["on_missing"]]
        return _result(check, status, value=value, expected=expected)

    passed = _vs_previous_passed(value, baseline, spec)
    status = _verdict(check, passed)
    return _result(check, status, value=value, expected=expected)


def _read_vs_previous_baseline(
    check: Check,
    now: datetime,
    calendar: BusinessCalendar | None,
    store: ObservationReader | None,
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
    configured guard must pass."""
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


def _to_aware_utc(value: Any, source_timezone: str = "UTC") -> datetime:
    """Coerce a DB timestamp to a tz-aware UTC datetime.

    A naive value is interpreted in ``source_timezone`` (the check's
    source's declared ``timezone:``, default UTC) before being
    converted; an already-aware value is unaffected.
    """
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(source_timezone))
    return value.astimezone(UTC)


def _connect_error_result(check: Check, exc: BaseException) -> Result:
    """An ERROR Result for a check whose source's adapter failed to build.

    Used for a source listed in ``failed_sources`` -- never indexes
    ``adapters`` for it, since no adapter exists.
    """
    result = _error_result(check, exc, label=_assertion_label(check))
    result.check_id = check_id(check)
    return result


def run_checks(
    adapters: dict[str, Adapter],
    checks: list[Check],
    calendar: BusinessCalendar | None = None,
    now: datetime | None = None,
    store: ObservationReader | None = None,
    failed_sources: dict[str, BaseException] | None = None,
    on_result: Callable[[Result], None] | None = None,
) -> RunResult:
    """Evaluate checks per source and aggregate the worst status.

    Sources run in parallel, one worker thread each; a source's own checks run
    serially on its single connection, which is never shared across threads.

    ``store`` is threaded straight through to :func:`evaluate_check` for
    history-based expectations; omit it and every run is otherwise identical
    to a run with no store (schema ``unchanged`` always passes).

    ``failed_sources`` maps a source name to the exception raised while
    building its adapter (see ``dbfresh.runner.run_and_persist``). Every
    check on such a source becomes a ``Status.ERROR`` Result carrying that
    exception's text, without ever indexing ``adapters`` for it -- other
    sources evaluate normally, so one unreachable source never blocks the
    rest of the run.

    ``on_result``, when given, is called once per check as its Result
    becomes available -- e.g. to advance a progress bar -- rather than once
    per source at the end. Sources evaluate concurrently on separate
    threads, so a callback that touches shared state must guard it itself.
    """
    now = now or datetime.now(UTC)
    failed_sources = failed_sources or {}
    by_source: dict[str, list[Check]] = {}
    for check in checks:
        by_source.setdefault(check.source, []).append(check)

    def run_source(source_checks: list[Check]) -> list[Result]:
        source = source_checks[0].source
        exc = failed_sources.get(source)
        results = []
        for check in source_checks:
            if exc is not None:
                result = _connect_error_result(check, exc)
            else:
                result = evaluate_check(check, adapters[source], now, calendar, store)
            log.debug(
                "check_result",
                check_id=result.check_id,
                object=result.object,
                source=source,
                status=str(result.status),
            )
            if result.status == Status.ERROR:
                log.error(
                    "check_error",
                    check_id=result.check_id,
                    object=result.object,
                    source=source,
                    error=result.error,
                )
            results.append(result)
            if on_result is not None:
                on_result(result)
        return results

    results: list[Result] = []
    if by_source:
        with ThreadPoolExecutor(max_workers=len(by_source)) as pool:
            for source_results in pool.map(run_source, by_source.values()):
                results.extend(source_results)
    return RunResult(results=results, status=worst_status(r.status for r in results))
