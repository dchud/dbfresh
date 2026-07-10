from dbfresh.adapters.base import Dialect
from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.checks import Check, compile_metric_sql, parse_expectation
from dbfresh.engine import Status, evaluate_check


def test_compile_null_rate_uses_dialect_float_form():
    check = Check(source="s", object="t", metric="null_rate", column="email")
    sql = compile_metric_sql(check, Dialect())
    assert sql == (
        "SELECT SUM(CASE WHEN email IS NULL THEN 1 ELSE 0 END) * 1.0 "
        "/ NULLIF(COUNT(*), 0) FROM t"
    )


def test_null_rate_computed_end_to_end():
    a = SqliteAdapter()
    a.rows("CREATE TABLE t (email TEXT)")
    a.rows("INSERT INTO t (email) VALUES ('a'), (NULL), (NULL), ('b')")
    check = Check(
        source="s",
        object="t",
        metric="null_rate",
        column="email",
        expect=parse_expectation({"max": 0.6}),
    )
    result = evaluate_check(check, a)
    assert result.value == 0.5
    assert result.status == Status.OK
    a.close()


def test_null_rate_on_empty_table_is_error():
    a = SqliteAdapter()
    a.rows("CREATE TABLE t (email TEXT)")
    check = Check(
        source="s",
        object="t",
        metric="null_rate",
        column="email",
        expect=parse_expectation({"max": 0.1}),
    )
    result = evaluate_check(check, a)
    assert result.status == Status.ERROR
    a.close()
