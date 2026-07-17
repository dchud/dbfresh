from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from dbfresh.report import format_timestamp, format_timestamp_friendly


def test_utc_datetime_gets_trailing_z():
    when = datetime(2026, 7, 12, 21, 23, 0, tzinfo=UTC)
    assert format_timestamp(when) == "2026-07-12T21:23:00Z"


def test_naive_datetime_is_assumed_utc():
    when = datetime(2026, 7, 12, 21, 23, 0)
    assert format_timestamp(when) == "2026-07-12T21:23:00Z"


def test_local_zone_shows_numeric_offset():
    when = datetime(2026, 7, 12, 21, 23, 0, tzinfo=UTC)
    text = format_timestamp(when, tz=ZoneInfo("America/New_York"))
    assert text == "2026-07-12T17:23:00-04:00"


def test_microseconds_are_dropped():
    when = datetime(2026, 7, 12, 21, 23, 0, 123456, tzinfo=UTC)
    assert format_timestamp(when) == "2026-07-12T21:23:00Z"


def test_converts_across_a_date_boundary():
    when = datetime(2026, 7, 13, 2, 30, 0, tzinfo=UTC)
    text = format_timestamp(when, tz=ZoneInfo("America/New_York"))
    assert text == "2026-07-12T22:30:00-04:00"


def test_friendly_shows_date_12h_time_and_weekday():
    when = datetime(2026, 7, 17, 14, 12, 0, tzinfo=UTC)
    assert format_timestamp_friendly(when) == "2026-07-17 2:12 PM (Fri)"


def test_friendly_midnight_is_12_am_and_noon_is_12_pm():
    assert (
        format_timestamp_friendly(datetime(2026, 7, 14, 0, 5, tzinfo=UTC))
        == "2026-07-14 12:05 AM (Tue)"
    )
    assert (
        format_timestamp_friendly(datetime(2026, 7, 14, 12, 0, tzinfo=UTC))
        == "2026-07-14 12:00 PM (Tue)"
    )


def test_friendly_converts_to_display_tz_without_printing_an_offset():
    when = datetime(2026, 7, 17, 15, 49, 0, tzinfo=UTC)
    # 15:49 UTC is 11:49 in New York (EDT); the weekday reflects local time
    assert (
        format_timestamp_friendly(when, tz=ZoneInfo("America/New_York"))
        == "2026-07-17 11:49 AM (Fri)"
    )
