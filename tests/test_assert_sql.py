"""`assert_sql:` -- a raw, author-supplied violation-selecting query, run
directly (distinct from `assert:`, a predicate compiled to
`SELECT * FROM obj WHERE NOT(pred)`). The dialect's row-limiting form caps
the single execution; the resulting row count is the persisted value.
"""

from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.checks import Check
from dbfresh.engine import Status, evaluate_check


def _adapter_with_negatives():
    a = SqliteAdapter()
    a.rows("CREATE TABLE fct (sale_id INTEGER, amount REAL)")
    a.rows("INSERT INTO fct VALUES (1, 10.0), (2, -5.0), (3, -1.0)")
    return a


def test_assert_sql_with_violations_fails():
    a = _adapter_with_negatives()
    check = Check(
        source="s", object="fct", assert_sql="SELECT * FROM fct WHERE amount < 0"
    )
    result = evaluate_check(check, a)
    assert result.status == Status.FAIL
    assert result.value == 2
    assert len(result.samples) == 2
    a.close()


def test_assert_sql_with_no_violations_is_ok():
    a = SqliteAdapter()
    a.rows("CREATE TABLE fct (amount REAL)")
    a.rows("INSERT INTO fct VALUES (1.0), (2.0)")
    check = Check(
        source="s", object="fct", assert_sql="SELECT * FROM fct WHERE amount < 0"
    )
    result = evaluate_check(check, a)
    assert result.status == Status.OK
    assert result.value == 0
    a.close()


def test_assert_sql_query_error_is_an_error_result():
    a = SqliteAdapter()
    check = Check(source="s", object="fct", assert_sql="SELECT * FROM nope")
    result = evaluate_check(check, a)
    assert result.status == Status.ERROR
    assert result.error is not None
    a.close()


def test_assert_sql_severity_warn_yields_warn_not_fail():
    a = _adapter_with_negatives()
    check = Check(
        source="s",
        object="fct",
        assert_sql="SELECT * FROM fct WHERE amount < 0",
        severity="warn",
    )
    result = evaluate_check(check, a)
    assert result.status == Status.WARN
