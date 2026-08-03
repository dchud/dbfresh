from datetime import UTC, datetime

from helpers import adapter_with_timestamp, freshness_check

from dbfresh.calendar import build_calendar
from dbfresh.engine import Status, evaluate_check


def test_business_calendar_passes_friday_data_checked_monday():
    a = adapter_with_timestamp("2026-07-03 18:00:00")  # Friday
    now = datetime(
        2026, 7, 6, 7, 0, tzinfo=UTC
    )  # Monday, ~61h wall-clock later
    cal = build_calendar({"timezone": "UTC"})
    result = evaluate_check(
        freshness_check(calendar="business"), a, now=now, calendar=cal
    )
    assert result.status == Status.OK
    assert result.value == 13 * 3600  # business lag, not wall-clock 61h
    a.close()


def test_wall_clock_freshness_fails_same_data_without_calendar_business():
    a = adapter_with_timestamp("2026-07-03 18:00:00")  # Friday
    now = datetime(2026, 7, 6, 7, 0, tzinfo=UTC)  # Monday
    cal = build_calendar({"timezone": "UTC"})
    result = evaluate_check(freshness_check(), a, now=now, calendar=cal)
    assert (
        result.status == Status.FAIL
    )  # 61h wall-clock lag, no calendar: business
    a.close()


def test_calendar_business_without_a_calendar_falls_back_to_wall_clock():
    a = adapter_with_timestamp("2026-07-03 18:00:00")  # Friday
    now = datetime(2026, 7, 6, 7, 0, tzinfo=UTC)  # Monday
    result = evaluate_check(freshness_check(calendar="business"), a, now=now)
    assert (
        result.status == Status.FAIL
    )  # no calendar passed, wall-clock 61h used
    a.close()
