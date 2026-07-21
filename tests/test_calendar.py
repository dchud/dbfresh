from datetime import UTC, date, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from dbfresh.calendar import build_calendar


def _calendar(**holiday_overrides):
    raw = {"timezone": "America/New_York"}
    if holiday_overrides:
        raw["holidays"] = holiday_overrides
    return build_calendar(raw)


def test_weekday_is_business_day():
    cal = _calendar()
    assert cal.is_business_day(date(2026, 7, 6)) is True  # Monday


def test_weekend_is_not_business_day():
    cal = _calendar()
    assert cal.is_business_day(date(2026, 7, 11)) is False  # Saturday
    assert cal.is_business_day(date(2026, 7, 12)) is False  # Sunday


def test_custom_workdays_include_saturday():
    cal = build_calendar(
        {
            "timezone": "America/New_York",
            "workdays": ["mon", "tue", "wed", "sat"],
        }
    )
    assert cal.is_business_day(date(2026, 7, 11)) is True  # Saturday
    assert cal.is_business_day(date(2026, 7, 9)) is False  # Thursday, dropped


def test_extra_holiday_is_not_business_day():
    cal = _calendar(extra=["2026-11-27"])
    assert cal.is_business_day(date(2026, 11, 27)) is False  # Friday
    assert cal.is_holiday(date(2026, 11, 27)) is True


def test_extra_holiday_does_not_affect_other_dates():
    cal = _calendar(extra=["2026-11-27"])
    assert cal.is_holiday(date(2026, 11, 26)) is False


def test_remove_overrides_extra_holiday():
    cal = _calendar(extra=["2026-11-27"], remove=["2026-11-27"])
    assert cal.is_holiday(date(2026, 11, 27)) is False
    assert cal.is_business_day(date(2026, 11, 27)) is True  # Friday, workday


def test_country_holiday_is_recognized():
    cal = _calendar(country="US")
    assert cal.is_holiday(date(2026, 7, 4)) is True  # Independence Day
    assert (
        cal.is_business_day(date(2026, 7, 4)) is False
    )  # Saturday too, but moot


def test_country_holiday_can_be_removed():
    cal = _calendar(country="US", remove=["2026-07-03"])
    assert (
        cal.is_holiday(date(2026, 7, 3)) is False
    )  # observed Friday, removed


def test_no_country_means_no_country_holidays():
    cal = _calendar()
    assert cal.is_holiday(date(2026, 7, 4)) is False


def test_subdivision_narrows_country_holidays():
    cal = _calendar(country="US", subdivision="CA")
    # A California state holiday not observed federally.
    assert cal.is_holiday(date(2026, 3, 31)) is True  # Cesar Chavez Day


def test_local_date_converts_to_calendar_timezone():
    cal = build_calendar({"timezone": "America/New_York"})
    when = datetime(2026, 7, 4, 2, 0, tzinfo=UTC)  # Fri 22:00 EDT
    assert cal.local_date(when) == date(2026, 7, 3)


def test_business_time_between_subtracts_weekend():
    cal = build_calendar({"timezone": "UTC"})
    t0 = datetime(2026, 7, 3, 18, 0, tzinfo=UTC)  # Friday
    t1 = datetime(2026, 7, 6, 7, 0, tzinfo=UTC)  # Monday
    assert cal.business_time_between(t0, t1) == timedelta(hours=13)


def test_business_time_between_same_business_day_is_wall_clock():
    cal = build_calendar({"timezone": "UTC"})
    t0 = datetime(2026, 7, 6, 9, 0, tzinfo=UTC)
    t1 = datetime(2026, 7, 6, 15, 0, tzinfo=UTC)
    assert cal.business_time_between(t0, t1) == timedelta(hours=6)


def test_business_time_between_subtracts_holiday_and_weekend():
    cal = build_calendar(
        {"timezone": "UTC", "holidays": {"extra": ["2026-07-03"]}}
    )
    t0 = datetime(2026, 7, 2, 18, 0, tzinfo=UTC)  # Thursday
    t1 = datetime(2026, 7, 6, 7, 0, tzinfo=UTC)  # Monday
    assert cal.business_time_between(t0, t1) == timedelta(hours=13)


def test_business_time_between_uses_calendar_timezone_not_input_tz():
    cal = build_calendar({"timezone": "America/New_York"})
    t0 = datetime(2026, 7, 4, 2, 0, tzinfo=UTC)  # Fri 22:00 EDT
    t1 = datetime(2026, 7, 6, 11, 0, tzinfo=UTC)  # Mon 07:00 EDT
    assert cal.business_time_between(t0, t1) == timedelta(hours=9)


@given(
    start=st.datetimes(
        min_value=datetime(2020, 1, 1), max_value=datetime(2030, 1, 1)
    ),
    elapsed_seconds=st.integers(min_value=0, max_value=60 * 60 * 24 * 30),
)
def test_business_time_between_stays_within_wall_clock_bounds(
    start, elapsed_seconds
):
    """Never negative, and never more than the raw wall-clock elapsed time."""
    cal = build_calendar({"timezone": "UTC"})
    t0 = start.replace(tzinfo=UTC)
    t1 = t0 + timedelta(seconds=elapsed_seconds)
    result = cal.business_time_between(t0, t1)
    assert timedelta(0) <= result <= (t1 - t0)


@given(
    start=st.datetimes(
        min_value=datetime(2020, 1, 1), max_value=datetime(2030, 1, 1)
    ),
    elapsed_seconds=st.integers(min_value=0, max_value=60 * 60 * 12),
)
def test_business_time_between_within_one_date_is_wall_clock(
    start, elapsed_seconds
):
    """No date boundary is crossed, so nothing is subtracted."""
    cal = build_calendar({"timezone": "UTC"})
    t0 = start.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=UTC)
    t1 = t0 + timedelta(seconds=elapsed_seconds)
    assert cal.business_time_between(t0, t1) == (t1 - t0)
