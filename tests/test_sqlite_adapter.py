import pytest

from dbfresh.adapters.sqlite import SqliteAdapter


def _seed(adapter):
    adapter.rows("CREATE TABLE t (id INTEGER PRIMARY KEY, email TEXT)")
    adapter.rows("INSERT INTO t (id, email) VALUES (1, 'x'), (2, NULL)")


def test_scalar_returns_single_value():
    a = SqliteAdapter()
    _seed(a)
    assert a.scalar("SELECT COUNT(*) FROM t") == 2
    a.close()


def test_rows_returns_dicts_preserving_nulls():
    a = SqliteAdapter()
    _seed(a)
    assert a.rows("SELECT id, email FROM t ORDER BY id") == [
        {"id": 1, "email": "x"},
        {"id": 2, "email": None},
    ]
    a.close()


def test_in_memory_state_persists_across_queries():
    # A single StaticPool connection keeps the in-memory database alive.
    a = SqliteAdapter()
    a.rows("CREATE TABLE t (id INTEGER)")
    a.rows("INSERT INTO t (id) VALUES (7)")
    assert a.scalar("SELECT MAX(id) FROM t") == 7
    a.close()


def _seed_n(adapter, n):
    adapter.rows("CREATE TABLE t (id INTEGER)")
    for i in range(n):
        adapter.rows(f"INSERT INTO t (id) VALUES ({i})")


def test_rows_limited_runs_sql_unmodified_and_caps_the_fetch():
    a = SqliteAdapter()
    _seed_n(a, 10)
    rows = a.rows_limited("SELECT * FROM t", 5)
    assert len(rows) == 5
    a.close()


def test_rows_limited_returns_every_row_when_under_the_cap():
    a = SqliteAdapter()
    _seed_n(a, 3)
    rows = a.rows_limited("SELECT * FROM t", 5)
    assert len(rows) == 3
    a.close()


def test_rows_limited_never_injects_a_sql_level_limit_clause():
    # The whole point: the SQL sent to the database is exactly what was
    # authored, never rewritten to inject a row cap -- the cap is applied
    # client-side via the cursor instead.
    a = SqliteAdapter()
    _seed_n(a, 10)
    queries = []
    original_execute = a._conn.execute

    def tracking_execute(clause, *args, **kwargs):
        queries.append(str(clause))
        return original_execute(clause, *args, **kwargs)

    a._conn.execute = tracking_execute
    a.rows_limited("SELECT * FROM t", 5)
    assert not any("LIMIT" in q for q in queries)
    a.close()


def test_rows_limited_handles_a_cte_without_truncating_the_scan():
    # A cap injected inside a CTE (the dialect-rewrite bug this replaces)
    # would truncate the scan before the outer WHERE ever sees the later
    # rows. Unmodified SQL sees every row the CTE selects.
    a = SqliteAdapter()
    a.rows("CREATE TABLE t (id INTEGER, amount REAL)")
    for i in range(20):
        a.rows(f"INSERT INTO t VALUES ({i}, 10.0)")
    for i in range(20, 25):
        a.rows(f"INSERT INTO t VALUES ({i}, -5.0)")
    sql = "WITH base AS (SELECT * FROM t) SELECT * FROM base WHERE amount < 0"
    rows = a.rows_limited(sql, 20)
    assert len(rows) == 5
    a.close()


def test_rows_limited_runs_select_distinct_unmangled():
    a = SqliteAdapter()
    a.rows("CREATE TABLE t (amount REAL)")
    a.rows("INSERT INTO t VALUES (1.0), (1.0), (2.0)")
    rows = a.rows_limited("SELECT DISTINCT amount FROM t", 20)
    assert len(rows) == 2
    a.close()


def test_rows_limited_returns_empty_list_for_ddl_with_no_rows():
    a = SqliteAdapter()
    rows = a.rows_limited("CREATE TABLE t (id INTEGER)", 20)
    assert rows == []
    a.close()


def test_close_disposes_engine_even_if_conn_close_raises():
    a = SqliteAdapter()
    disposed = []
    original_dispose = a._engine.dispose

    def tracking_dispose(*args, **kwargs):
        disposed.append(True)
        original_dispose(*args, **kwargs)

    def raising_close():
        raise RuntimeError("boom on close")

    a._engine.dispose = tracking_dispose
    a._conn.close = raising_close

    with pytest.raises(RuntimeError, match="boom on close"):
        a.close()

    assert disposed == [True]
