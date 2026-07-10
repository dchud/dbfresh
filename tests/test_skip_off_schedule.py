from datetime import UTC, datetime

from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.calendar import build_calendar
from dbfresh.checks import Check, parse_expectation
from dbfresh.engine import Status, evaluate_check


def _rows_adapter(n):
    a = SqliteAdapter()
    a.rows("CREATE TABLE t (id INTEGER)")
    for i in range(n):
        a.rows(f"INSERT INTO t (id) VALUES ({i})")
    return a


def _calendar():
    return build_calendar({"timezone": "UTC"})


def test_skip_off_schedule_skips_on_weekend():
    a = _rows_adapter(0)  # would ERROR/FAIL if evaluated: null_rate-free row_count ok
    check = Check(
        source="s",
        object="t",
        metric="row_count",
        expect=parse_expectation({"min": 1}),  # 0 rows would fail
        skip_off_schedule=True,
    )
    now = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)  # Saturday
    result = evaluate_check(check, a, now=now, calendar=_calendar())
    assert result.status == Status.SKIPPED
    a.close()


def test_skip_off_schedule_skips_on_holiday():
    a = _rows_adapter(0)
    check = Check(
        source="s",
        object="t",
        metric="row_count",
        expect=parse_expectation({"min": 1}),
        skip_off_schedule=True,
    )
    cal = build_calendar({"timezone": "UTC", "holidays": {"extra": ["2026-07-06"]}})
    now = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)  # Monday holiday
    result = evaluate_check(check, a, now=now, calendar=cal)
    assert result.status == Status.SKIPPED
    a.close()


def test_skip_off_schedule_runs_normally_on_business_day():
    a = _rows_adapter(0)
    check = Check(
        source="s",
        object="t",
        metric="row_count",
        expect=parse_expectation({"min": 1}),
        skip_off_schedule=True,
    )
    now = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)  # Monday, a business day
    result = evaluate_check(check, a, now=now, calendar=_calendar())
    assert result.status == Status.FAIL  # evaluated normally, 0 rows fails min:1
    a.close()


def test_skip_off_schedule_false_runs_even_off_schedule():
    a = _rows_adapter(0)
    check = Check(
        source="s",
        object="t",
        metric="row_count",
        expect=parse_expectation({"min": 1}),
        skip_off_schedule=False,
    )
    now = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)  # Saturday
    result = evaluate_check(check, a, now=now, calendar=_calendar())
    assert result.status == Status.FAIL
    a.close()


def test_skipped_status_excluded_from_failure_counts_via_worst_status():
    from dbfresh.engine import worst_status

    assert worst_status([Status.SKIPPED, Status.SKIPPED]) == Status.OK
