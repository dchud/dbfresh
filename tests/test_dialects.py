from dbfresh.adapters.base import Dialect
from dbfresh.adapters.databricks import DatabricksDialect
from dbfresh.adapters.sqlserver import TSqlDialect


def test_base_dialect_uses_limit():
    assert Dialect().limit("SELECT * FROM t", 20) == "SELECT * FROM t LIMIT 20"


def test_tsql_dialect_uses_top():
    sql = TSqlDialect().limit("SELECT * FROM t WHERE NOT (x >= 0)", 20)
    assert sql == "SELECT TOP 20 * FROM t WHERE NOT (x >= 0)"


def test_base_freshness_capability_is_column_only():
    assert Dialect().freshness_sources == frozenset({"column"})


def test_base_introspection_capabilities_is_empty():
    assert Dialect().introspection_capabilities == frozenset()


def test_databricks_adds_describe_freshness_capabilities():
    caps = DatabricksDialect().freshness_sources
    assert caps == frozenset({"column", "describe_history", "describe_detail"})
