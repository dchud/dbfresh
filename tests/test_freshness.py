from datetime import UTC, date, datetime, time, timedelta, timezone

from dbfresh.adapters.base import Dialect
from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.checks import Check, compile_metric_sql, parse_expectation
from dbfresh.engine import Status, evaluate_check


def test_compile_freshness():
    check = Check(
        source="s", object="t", metric="freshness", column="created_at"
    )
    assert (
        compile_metric_sql(check, Dialect()) == "SELECT MAX(created_at) FROM t"
    )


def test_max_lag_describe():
    assert parse_expectation({"max_lag": "26h"}).describe() == "max_lag 26h"


def _freshness_check():
    return Check(
        source="s",
        object="t",
        metric="freshness",
        column="created_at",
        expect=parse_expectation({"max_lag": "26h"}),
    )


def _adapter_with_timestamp(value):
    a = SqliteAdapter()
    a.rows("CREATE TABLE t (created_at TEXT)")
    a.rows(f"INSERT INTO t (created_at) VALUES ('{value}')")
    return a


def test_fresh_data_passes():
    a = _adapter_with_timestamp("2026-07-10 10:00:00")
    now = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)  # 10h later
    result = evaluate_check(_freshness_check(), a, now=now)
    assert result.status == Status.OK
    assert result.value == 36000  # 10h in seconds
    a.close()


def test_stale_data_fails():
    a = _adapter_with_timestamp("2026-07-08 10:00:00")
    now = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)  # ~58h later
    result = evaluate_check(_freshness_check(), a, now=now)
    assert result.status == Status.FAIL
    a.close()


def test_empty_table_freshness_fails():
    a = SqliteAdapter()
    a.rows("CREATE TABLE t (created_at TEXT)")
    now = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)
    result = evaluate_check(_freshness_check(), a, now=now)
    assert result.status == Status.FAIL
    a.close()


class _ScalarAdapter:
    """A minimal adapter whose ``MAX(column)`` returns a fixed value --
    drives freshness evaluation with whatever object a driver yields for a
    given column type (a ``date``, an aware ``datetime`` for
    ``datetimeoffset``, a ``time``, ...)."""

    dialect = Dialect()

    def __init__(self, value):
        self._value = value

    def scalar(self, sql):
        return self._value


def test_freshness_on_a_date_column_measures_lag_from_midnight():
    # A DATE column has no time-of-day; the day is treated as midnight in
    # the source timezone (UTC here), not rejected for lacking a tzinfo.
    a = _ScalarAdapter(date(2026, 7, 10))
    now = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)
    result = evaluate_check(_freshness_check(), a, now=now)
    assert result.status == Status.OK
    assert result.value == 20 * 3600  # 20h since midnight UTC


def test_freshness_date_column_uses_the_source_timezone_for_midnight():
    check = _freshness_check()
    check.source_timezone = "America/New_York"  # EDT (UTC-4) in July
    a = _ScalarAdapter(date(2026, 7, 10))
    now = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)  # midnight NY = 04:00 UTC
    result = evaluate_check(check, a, now=now)
    assert result.value == 16 * 3600  # 20:00 - 04:00 = 16h


def test_freshness_on_an_aware_datetime_uses_its_own_offset():
    # datetimeoffset comes back as an aware datetime; its own offset is
    # used to convert to UTC, not the source's declared timezone.
    value = datetime(2026, 7, 10, 10, 0, tzinfo=timezone(timedelta(hours=-4)))
    check = _freshness_check()
    check.source_timezone = "Asia/Tokyo"  # deliberately different; ignored
    a = _ScalarAdapter(value)
    now = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)  # 10:00-04:00 = 14:00 UTC
    result = evaluate_check(check, a, now=now)
    assert result.value == 6 * 3600  # 20:00 - 14:00 UTC = 6h


def test_freshness_on_a_non_temporal_value_is_a_clear_error():
    a = _ScalarAdapter(time(14, 30))  # a TIME column: no date, no age
    now = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)
    result = evaluate_check(_freshness_check(), a, now=now)
    assert result.status == Status.ERROR
    assert "not a date or datetime" in (result.error or "")


def test_freshness_on_a_numeric_column_is_rejected_before_the_query_runs():
    a = SqliteAdapter()
    a.rows("CREATE TABLE t (created_at INTEGER)")
    a.rows("INSERT INTO t (created_at) VALUES (20260710)")
    now = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)
    result = evaluate_check(_freshness_check(), a, now=now)
    assert result.status == Status.ERROR
    assert "created_at" in (result.error or "")
    assert "INTEGER" in (result.error or "")
    # The describe-time check must fire before MAX() runs -- not the
    # runtime _to_aware_utc guard, which would say "returned a int".
    assert "returned a int" not in (result.error or "")
    a.close()


class _DescribeRaisingSqliteAdapter(SqliteAdapter):
    """A SqliteAdapter whose ``describe`` always fails -- simulates a source
    with query access but no metadata/catalog access."""

    def describe(self, obj):
        raise RuntimeError("metadata unavailable")


def test_freshness_column_check_is_best_effort_when_describe_fails():
    a = _DescribeRaisingSqliteAdapter()
    a.rows("CREATE TABLE t (created_at TEXT)")
    a.rows("INSERT INTO t (created_at) VALUES ('2026-07-10 10:00:00')")
    now = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)  # 10h later
    result = evaluate_check(_freshness_check(), a, now=now)
    assert result.status == Status.OK
    a.close()
