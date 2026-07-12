from datetime import UTC, datetime

from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.checks import Check, check_id, parse_expectation
from dbfresh.engine import Status, evaluate_check


def test_ok_result_carries_check_id():
    a = SqliteAdapter()
    a.rows("CREATE TABLE t (id INTEGER)")
    a.rows("INSERT INTO t (id) VALUES (1), (2)")
    check = Check(
        source="s",
        object="t",
        metric="row_count",
        expect=parse_expectation({"between": [1, 10]}),
    )
    result = evaluate_check(check, a)
    assert result.status == Status.OK
    assert result.check_id == check_id(check)
    a.close()


def test_error_result_carries_check_id():
    a = SqliteAdapter()  # table never created
    check = Check(
        source="s",
        object="missing",
        metric="row_count",
        expect=parse_expectation({"max": 10}),
    )
    result = evaluate_check(check, a)
    assert result.status == Status.ERROR
    assert result.check_id == check_id(check)
    a.close()


def test_assertion_result_carries_check_id():
    a = SqliteAdapter()
    a.rows("CREATE TABLE fct (amount REAL)")
    a.rows("INSERT INTO fct VALUES (-1.0)")
    check = Check(source="s", object="fct", assert_="amount >= 0")
    result = evaluate_check(check, a)
    assert result.status == Status.FAIL
    assert result.check_id == check_id(check)
    a.close()


def test_freshness_result_carries_check_id():
    a = SqliteAdapter()
    a.rows("CREATE TABLE t (created_at TEXT)")
    a.rows("INSERT INTO t (created_at) VALUES ('2026-07-10 10:00:00')")
    check = Check(
        source="s",
        object="t",
        metric="freshness",
        column="created_at",
        expect=parse_expectation({"max_lag": "26h"}),
    )
    now = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)
    result = evaluate_check(check, a, now=now)
    assert result.check_id == check_id(check)
    a.close()


def test_empty_result_carries_check_id():
    a = SqliteAdapter()
    a.rows("CREATE TABLE t (id INTEGER)")
    check = Check(
        source="s",
        object="t",
        metric="row_count",
        expect=parse_expectation({"max": 10}),
    )
    result = evaluate_check(check, a)
    assert result.check_id == check_id(check)
    a.close()
