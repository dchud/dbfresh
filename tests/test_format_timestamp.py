from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from dbfresh.report import format_timestamp


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
