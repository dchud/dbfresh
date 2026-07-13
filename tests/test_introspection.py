from dbfresh.adapters.base import Category, _split_object
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


def test_split_object_two_part_name():
    assert _split_object("dbo.fct_sales") == ("dbo", "fct_sales")


def test_split_object_no_schema():
    assert _split_object("fct_sales") == (None, "fct_sales")


def test_split_object_three_part_name_keeps_db_and_schema_together():
    # Splits at the *last* dot -- not a bug for SQL Server: its SQLAlchemy
    # dialect re-splits a dotted schema string itself as database.owner,
    # so "db.schema" reflects correctly as a compound schema (see
    # test_sqlserver_adapter.py's mocked-reflection wiring test).
    assert _split_object("db.schema.fct_sales") == ("db.schema", "fct_sales")
