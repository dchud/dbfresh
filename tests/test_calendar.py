from datetime import date

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
        {"timezone": "America/New_York", "workdays": ["mon", "tue", "wed", "sat"]}
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
    assert cal.is_business_day(date(2026, 7, 4)) is False  # Saturday too, but moot


def test_country_holiday_can_be_removed():
    cal = _calendar(country="US", remove=["2026-07-03"])
    assert cal.is_holiday(date(2026, 7, 3)) is False  # observed Friday, removed


def test_no_country_means_no_country_holidays():
    cal = _calendar()
    assert cal.is_holiday(date(2026, 7, 4)) is False


def test_subdivision_narrows_country_holidays():
    cal = _calendar(country="US", subdivision="CA")
    # A California state holiday not observed federally.
    assert cal.is_holiday(date(2026, 3, 31)) is True  # Cesar Chavez Day
