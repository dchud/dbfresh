from datetime import UTC, datetime

from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.calendar import build_calendar
from dbfresh.checks import Check, parse_expectation
from dbfresh.engine import Status, evaluate_check, run_checks


def _rows_adapter(n):
    a = SqliteAdapter()
    a.rows("CREATE TABLE t (id INTEGER)")
    for i in range(n):
        a.rows(f"INSERT INTO t (id) VALUES ({i})")
    return a


def _calendar(**holiday_overrides):
    raw = {"timezone": "UTC"}
    if holiday_overrides:
        raw["holidays"] = holiday_overrides
    return build_calendar(raw)


def test_by_weekday_override_selected_for_run_date():
    a = _rows_adapter(5)
    check = Check(
        source="s",
        object="t",
        metric="row_count",
        expect=parse_expectation({"max": 1}),  # base would fail with 5 rows
        by_weekday={"mon": parse_expectation({"max": 10})},  # override passes
    )
    now = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)  # Monday
    result = evaluate_check(check, a, now=now, calendar=_calendar())
    assert result.status == Status.OK
    a.close()


def test_by_weekday_falls_back_to_base_expect_when_no_match():
    a = _rows_adapter(5)
    check = Check(
        source="s",
        object="t",
        metric="row_count",
        expect=parse_expectation({"max": 1}),
        by_weekday={"mon": parse_expectation({"max": 10})},
    )
    now = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)  # Tuesday, no override
    result = evaluate_check(check, a, now=now, calendar=_calendar())
    assert result.status == Status.FAIL
    a.close()


def test_on_holiday_takes_precedence_over_by_weekday():
    a = _rows_adapter(5)
    check = Check(
        source="s",
        object="t",
        metric="row_count",
        expect=parse_expectation({"max": 1}),
        by_weekday={"mon": parse_expectation({"max": 1})},  # would still fail
        on_holiday=parse_expectation({"max": 10}),  # passes
    )
    cal = _calendar(extra=["2026-07-06"])
    now = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)  # Monday, also a holiday
    result = evaluate_check(check, a, now=now, calendar=cal)
    assert result.status == Status.OK
    a.close()


def test_on_holiday_not_used_when_today_is_not_a_holiday():
    a = _rows_adapter(5)
    check = Check(
        source="s",
        object="t",
        metric="row_count",
        expect=parse_expectation({"max": 1}),
        on_holiday=parse_expectation({"max": 10}),
    )
    now = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)  # Monday, not a holiday
    result = evaluate_check(check, a, now=now, calendar=_calendar())
    assert result.status == Status.FAIL
    a.close()


def test_run_checks_threads_calendar_and_now_through():
    a = _rows_adapter(5)
    check = Check(
        source="s",
        object="t",
        metric="row_count",
        expect=parse_expectation({"max": 1}),
        by_weekday={"mon": parse_expectation({"max": 10})},
    )
    now = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)  # Monday
    run = run_checks({"s": a}, [check], calendar=_calendar(), now=now)
    assert run.results[0].status == Status.OK
    a.close()


def test_without_calendar_by_weekday_is_ignored():
    a = _rows_adapter(5)
    check = Check(
        source="s",
        object="t",
        metric="row_count",
        expect=parse_expectation({"max": 1}),
        by_weekday={"mon": parse_expectation({"max": 10})},
    )
    now = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)  # Monday
    result = evaluate_check(check, a, now=now)  # no calendar passed
    assert result.status == Status.FAIL  # base expect used, 5 > 1
    a.close()
