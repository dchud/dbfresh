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
