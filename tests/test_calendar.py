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
