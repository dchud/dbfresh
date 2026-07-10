from dbfresh.adapters.base import Dialect
from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.checks import Check, compile_metric_sql, parse_expectation
from dbfresh.engine import Status, evaluate_check


def test_compile_duplicate_count_guards_nulls():
    check = Check(source="s", object="t", metric="duplicate_count", key="id")
    sql = compile_metric_sql(check, Dialect())
    assert sql == "SELECT COUNT(*) - COUNT(DISTINCT id) FROM t WHERE id IS NOT NULL"


def test_compile_duplicate_count_with_where():
    check = Check(
        source="s", object="t", metric="duplicate_count", key="id", where="active = 1"
    )
    sql = compile_metric_sql(check, Dialect())
    assert sql == (
        "SELECT COUNT(*) - COUNT(DISTINCT id) FROM t "
        "WHERE id IS NOT NULL AND active = 1"
    )


def test_compile_aggregate():
    check = Check(source="s", object="t", metric="avg", column="amount")
    assert compile_metric_sql(check, Dialect()) == "SELECT AVG(amount) FROM t"


def test_duplicate_count_end_to_end():
    a = SqliteAdapter()
    a.rows("CREATE TABLE t (id INTEGER)")
    a.rows("INSERT INTO t (id) VALUES (1), (2), (2), (3), (NULL)")
    check = Check(
        source="s",
        object="t",
        metric="duplicate_count",
        key="id",
        expect=parse_expectation({"max": 0}),
    )
    result = evaluate_check(check, a)
    assert result.value == 1  # one duplicated key, nulls excluded
    assert result.status == Status.FAIL
    a.close()


def test_avg_end_to_end():
    a = SqliteAdapter()
    a.rows("CREATE TABLE t (amount REAL)")
    a.rows("INSERT INTO t (amount) VALUES (10), (20), (30)")
    check = Check(
        source="s",
        object="t",
        metric="avg",
        column="amount",
        expect=parse_expectation({"between": [15, 25]}),
    )
    result = evaluate_check(check, a)
    assert result.value == 20
    assert result.status == Status.OK
    a.close()
