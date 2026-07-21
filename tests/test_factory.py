import pytest

from dbfresh.adapters.factory import (
    MissingDriverError,
    adapter_class_for,
    create_adapter,
    supported_types,
)
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
    # rather than raising "unknown source type", and that the raw
    # ModuleNotFoundError is reworded into a hint naming the extra.
    with pytest.raises(MissingDriverError) as exc_info:
        create_adapter("sqlserver", {"url": "sqlserver://user:pass@host/db"})
    message = str(exc_info.value)
    assert "pymssql" in message
    assert "dbfresh[sqlserver]" in message


def test_supported_types_lists_every_registered_type_sorted():
    assert supported_types() == [
        "databricks",
        "postgres",
        "sqlite",
        "sqlserver",
    ]


def test_databricks_type_resolves_to_the_databricks_adapter():
    # databricks-sql-connector isn't installed in this test environment, so
    # construction fails past dispatch -- proving the factory routed to
    # DatabricksAdapter rather than raising "unknown source type", reworded
    # into a hint naming the extra.
    with pytest.raises(MissingDriverError) as exc_info:
        create_adapter(
            "databricks",
            {
                "host": "x",
                "http_path": "/sql/1.0/warehouses/abc",
                "token": "t",
            },
        )
    assert "dbfresh[databricks]" in str(exc_info.value)


def test_create_adapter_rewords_a_missing_submodule_of_the_driver_package(
    monkeypatch,
):
    # The oauth_m2m path imports databricks.sdk, a submodule of the same
    # "databricks" driver package as databricks.sql -- a workspace with
    # only the connector installed hits this, not the exact "databricks"
    # top-level name the plain equality check was written for.
    from dbfresh.adapters import factory

    class _MissingSdkSubmodule:
        def __init__(self, **kwargs):
            raise ModuleNotFoundError(
                "No module named 'databricks.sdk'", name="databricks.sdk"
            )

    monkeypatch.setitem(factory._ADAPTERS, "databricks", _MissingSdkSubmodule)
    with pytest.raises(MissingDriverError) as exc_info:
        create_adapter("databricks", {"host": "x"})
    assert "dbfresh[databricks]" in str(exc_info.value)


def test_create_adapter_does_not_reword_an_unrelated_missing_module(
    monkeypatch,
):
    # Only the source type's own driver module is reworded; any other
    # ModuleNotFoundError (e.g. a genuinely broken import) propagates as-is.
    from dbfresh.adapters import factory

    class _MissingSomethingElse:
        def __init__(self, **kwargs):
            raise ModuleNotFoundError(
                "No module named 'somethingelse'", name="somethingelse"
            )

    monkeypatch.setitem(factory._ADAPTERS, "sqlserver", _MissingSomethingElse)
    with pytest.raises(ModuleNotFoundError) as exc_info:
        create_adapter("sqlserver", {"url": "x"})
    assert not isinstance(exc_info.value, MissingDriverError)
