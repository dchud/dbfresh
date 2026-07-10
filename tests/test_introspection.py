from dbfresh.adapters.base import Category
from dbfresh.adapters.sqlite import SqliteAdapter


def test_describe_columns_carry_category_and_nullable():
    a = SqliteAdapter()
    a.rows(
        "CREATE TABLE fct ("
        "  id INTEGER PRIMARY KEY,"
        "  email TEXT NOT NULL,"
        "  amount REAL,"
        "  created_at TIMESTAMP"
        ")"
    )
    info = a.describe("fct")
    by_name = {c.name: c for c in info.columns}

    assert by_name["id"].category == Category.NUMERIC
    assert by_name["email"].category == Category.STRING
    assert by_name["email"].nullable is False
    assert by_name["amount"].category == Category.NUMERIC
    assert by_name["amount"].nullable is True
    assert by_name["created_at"].category == Category.TEMPORAL
    a.close()


def test_describe_keys_from_primary_key():
    a = SqliteAdapter()
    a.rows("CREATE TABLE t (id INTEGER PRIMARY KEY, email TEXT)")
    info = a.describe("t")
    assert info.keys == [["id"]]
    a.close()


def test_describe_no_keys_is_none():
    a = SqliteAdapter()
    a.rows("CREATE TABLE t (a INTEGER, b TEXT)")
    info = a.describe("t")
    assert info.keys is None
    a.close()
