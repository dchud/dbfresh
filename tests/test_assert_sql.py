"""`assert_sql:` -- a raw, author-supplied violation-selecting query, run
unmodified (distinct from `assert:`, a predicate compiled to
`SELECT * FROM obj WHERE NOT(pred)`). Never rewritten via the dialect's
row-limiting form -- that rewrite corrupts author SQL (a cap injected
inside a CTE, `SELECT DISTINCT` mangled into invalid syntax) -- capped
instead at fetch time via `rows_limited`; the fetched row count is the
persisted value below the cap, and a capped ("N+") value above it.
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


def _adapter_with_n_negatives(n, positives=0):
    a = SqliteAdapter()
    a.rows("CREATE TABLE fct (sale_id INTEGER, amount REAL)")
    for i in range(positives):
        a.rows(f"INSERT INTO fct VALUES ({i}, 10.0)")
    for i in range(positives, positives + n):
        a.rows(f"INSERT INTO fct VALUES ({i}, -5.0)")
    return a


def test_assert_sql_cte_with_broad_violations_is_not_truncated_inside_the_cte():
    # The first 20 rows are benign; the violations are rows 21-30. A cap
    # injected inside the CTE (the old dialect-rewrite bug) would truncate
    # the scan before those later rows are ever reached, reporting zero
    # violations instead of the true count.
    a = _adapter_with_n_negatives(n=10, positives=20)
    check = Check(
        source="s",
        object="fct",
        assert_sql=(
            "WITH base AS (SELECT * FROM fct) SELECT * FROM base WHERE amount < 0"
        ),
    )
    result = evaluate_check(check, a)
    assert result.status == Status.FAIL
    assert result.value == 10
    a.close()


def test_assert_sql_fetch_cap_is_honored_for_more_than_cap_violations():
    a = _adapter_with_n_negatives(n=25)
    check = Check(
        source="s", object="fct", assert_sql="SELECT * FROM fct WHERE amount < 0"
    )
    result = evaluate_check(check, a)
    assert result.status == Status.FAIL
    assert result.value == "20+"
    assert len(result.samples) == 10
    a.close()


def test_assert_sql_fewer_than_cap_violations_is_an_exact_count():
    a = _adapter_with_n_negatives(n=15)
    check = Check(
        source="s", object="fct", assert_sql="SELECT * FROM fct WHERE amount < 0"
    )
    result = evaluate_check(check, a)
    assert result.status == Status.FAIL
    assert result.value == 15
    a.close()


def test_assert_sql_select_distinct_runs_without_being_mangled():
    a = SqliteAdapter()
    a.rows("CREATE TABLE fct (amount REAL)")
    a.rows("INSERT INTO fct VALUES (-1.0), (-1.0), (-2.0)")
    check = Check(
        source="s",
        object="fct",
        assert_sql="SELECT DISTINCT amount FROM fct WHERE amount < 0",
    )
    result = evaluate_check(check, a)
    assert result.status == Status.FAIL
    assert result.value == 2
    a.close()
