from datetime import timedelta

import pytest

from dbfresh.checks import parse_duration


def test_hours():
    assert parse_duration("26h") == timedelta(hours=26)


def test_days():
    assert parse_duration("2d") == timedelta(days=2)


def test_minutes():
    assert parse_duration("90m") == timedelta(minutes=90)


def test_seconds():
    assert parse_duration("45s") == timedelta(seconds=45)


def test_compound():
    assert parse_duration("1h30m") == timedelta(hours=1, minutes=30)


def test_compound_all_units():
    assert parse_duration("1d2h3m4s") == timedelta(
        days=1, hours=2, minutes=3, seconds=4
    )


def test_whitespace_tolerated():
    assert parse_duration("  26h  ") == timedelta(hours=26)


def test_unknown_unit_raises():
    with pytest.raises(ValueError):
        parse_duration("5y")


def test_garbage_raises():
    with pytest.raises(ValueError):
        parse_duration("nonsense")


def test_number_without_unit_raises():
    with pytest.raises(ValueError):
        parse_duration("1h30")


def test_empty_raises():
    with pytest.raises(ValueError):
        parse_duration("")
