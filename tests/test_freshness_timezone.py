"""A naive source timestamp is interpreted in the source's timezone (default
UTC) before freshness lag is computed -- SQL Server datetime columns are
typically naive, so a non-UTC source needs this to avoid an offset error
equal to its UTC difference. An aware timestamp is unaffected.
"""

from datetime import UTC, datetime, timedelta, timezone

from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.checks import Check, parse_expectation
from dbfresh.engine import evaluate_check


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


def test_naive_timestamp_defaults_to_utc_when_source_timezone_unset():
    a = _adapter_with_timestamp("2026-07-10 10:00:00")
    now = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)  # 10h later, if UTC
    result = evaluate_check(_freshness_check(), a, now=now)
    assert result.value == 10 * 3600
    a.close()


def test_naive_timestamp_interpreted_in_source_timezone():
    # 2026-07-10 10:00 America/New_York (EDT, UTC-4) == 14:00 UTC.
    a = _adapter_with_timestamp("2026-07-10 10:00:00")
    now = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)
    check = _freshness_check(source_timezone="America/New_York")
    result = evaluate_check(check, a, now=now)
    assert result.value == 6 * 3600  # 20:00 - 14:00 UTC, not 10h
    a.close()


def test_aware_timestamp_is_unaffected_by_source_timezone():
    a = SqliteAdapter()
    a.rows("CREATE TABLE t (created_at TEXT)")
    # 14:00-04:00 is 18:00 UTC -- an explicit offset, so source_timezone
    # must be ignored entirely (unlike the naive-string cases above).
    aware = datetime(2026, 7, 10, 14, 0, tzinfo=timezone(timedelta(hours=-4)))
    a.rows(f"INSERT INTO t (created_at) VALUES ('{aware.isoformat()}')")
    now = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)
    check = _freshness_check(source_timezone="America/New_York")
    result = evaluate_check(check, a, now=now)
    assert result.value == 2 * 3600  # 20:00 - 18:00 UTC
    a.close()


def test_check_source_timezone_defaults_to_utc():
    check = Check(source="s", object="t", metric="freshness", column="c")
    assert check.source_timezone == "UTC"
