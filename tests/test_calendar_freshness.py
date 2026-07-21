from datetime import UTC, datetime

from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.calendar import build_calendar
from dbfresh.checks import Check, parse_expectation
from dbfresh.engine import Status, evaluate_check


def _adapter_with_timestamp(value):
    a = SqliteAdapter()
    a.rows("CREATE TABLE t (created_at TEXT)")
    a.rows(f"INSERT INTO t (created_at) VALUES ('{value}')")
    return a


def _freshness_check(**overrides):
    return Check(
        source="s",
        object="t",
        metric="freshness",
        column="created_at",
        expect=parse_expectation({"max_lag": "26h"}),
        **overrides,
    )


def test_business_calendar_passes_friday_data_checked_monday():
    a = _adapter_with_timestamp("2026-07-03 18:00:00")  # Friday
    now = datetime(
        2026, 7, 6, 7, 0, tzinfo=UTC
    )  # Monday, ~61h wall-clock later
    cal = build_calendar({"timezone": "UTC"})
    result = evaluate_check(
        _freshness_check(calendar="business"), a, now=now, calendar=cal
    )
    assert result.status == Status.OK
    assert result.value == 13 * 3600  # business lag, not wall-clock 61h
    a.close()


def test_wall_clock_freshness_fails_same_data_without_calendar_business():
    a = _adapter_with_timestamp("2026-07-03 18:00:00")  # Friday
    now = datetime(2026, 7, 6, 7, 0, tzinfo=UTC)  # Monday
    cal = build_calendar({"timezone": "UTC"})
    result = evaluate_check(_freshness_check(), a, now=now, calendar=cal)
    assert (
        result.status == Status.FAIL
    )  # 61h wall-clock lag, no calendar: business
    a.close()


def test_calendar_business_without_a_calendar_falls_back_to_wall_clock():
    a = _adapter_with_timestamp("2026-07-03 18:00:00")  # Friday
    now = datetime(2026, 7, 6, 7, 0, tzinfo=UTC)  # Monday
    result = evaluate_check(_freshness_check(calendar="business"), a, now=now)
    assert (
        result.status == Status.FAIL
    )  # no calendar passed, wall-clock 61h used
    a.close()
