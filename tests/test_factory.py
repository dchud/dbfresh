import pytest

from dbfresh.adapters.factory import create_adapter


def test_create_sqlite_adapter_works():
    adapter = create_adapter("sqlite", {"database": ":memory:"})
    assert adapter.scalar("SELECT 1") == 1
    adapter.close()


def test_unknown_type_raises():
    with pytest.raises(ValueError):
        create_adapter("mystery", {})


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
