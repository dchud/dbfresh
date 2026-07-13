import pytest

from dbfresh.adapters.factory import adapter_class_for, create_adapter
from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.adapters.sqlserver import SqlServerAdapter


def test_create_sqlite_adapter_works():
    adapter = create_adapter("sqlite", {"database": ":memory:"})
    assert adapter.scalar("SELECT 1") == 1
    adapter.close()


def test_unknown_type_raises():
    with pytest.raises(ValueError):
        create_adapter("mystery", {})


def test_adapter_class_for_returns_the_registered_class_without_constructing():
    assert adapter_class_for("sqlite") is SqliteAdapter
    assert adapter_class_for("sqlserver") is SqlServerAdapter


def test_adapter_class_for_unknown_type_raises():
    with pytest.raises(ValueError):
        adapter_class_for("mystery")


def test_create_adapter_ignores_timeout_for_an_adapter_that_does_not_accept_one():
    # SqliteAdapter has no `timeout` parameter -- passing one must not
    # raise a TypeError, it's simply never forwarded.
    adapter = create_adapter("sqlite", {"database": ":memory:"}, timeout=5)
    assert adapter.scalar("SELECT 1") == 1
    adapter.close()


def test_sqlserver_type_resolves_to_the_sqlserver_adapter():
    # pymssql isn't installed in this test environment, so construction
    # fails past dispatch -- proving the factory routed to SqlServerAdapter
    # rather than raising "unknown source type".
    with pytest.raises(ModuleNotFoundError):
        create_adapter("sqlserver", {"url": "sqlserver://user:pass@host/db"})


def test_databricks_type_resolves_to_the_databricks_adapter():
    # databricks-sql-connector isn't installed in this test environment, so
    # construction fails past dispatch -- proving the factory routed to
    # DatabricksAdapter rather than raising "unknown source type".
    with pytest.raises(ModuleNotFoundError):
        create_adapter(
            "databricks",
            {"host": "x", "http_path": "/sql/1.0/warehouses/abc", "token": "t"},
        )
