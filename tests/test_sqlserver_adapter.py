"""Contract-level tests for the pymssql-backed SQL Server adapter.

No live SQL Server is required: the dialect, category-mapping, and SQL
compilation tests are pure; ``describe()`` is exercised against a mocked
reflection ``Inspector`` and a stubbed ``scalar`` (the partition-stats row
count query). A live round-trip sits behind ``DBFRESH_SQLSERVER_URL`` and is
skipped unless that env var is set.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.dialects import mssql

from dbfresh.adapters.base import Category
from dbfresh.adapters.sqlserver import SqlServerAdapter, TSqlDialect, refine_category
from dbfresh.checks import Check, compile_metric_sql

SQLSERVER_URL = os.environ.get("DBFRESH_SQLSERVER_URL")


def test_module_imports_without_pymssql_installed():
    # pymssql is an optional extra, not a core dependency; this test
    # environment doesn't install it, so a bare import proves the module has
    # no module-level `import pymssql`. Constructing an adapter still needs
    # the driver -- see test_constructing_adapter_needs_pymssql below.
    import dbfresh.adapters.sqlserver as _  # noqa: F401


def test_constructing_adapter_needs_pymssql_only_at_connect_time():
    with pytest.raises(ModuleNotFoundError):
        SqlServerAdapter("sqlserver://user:pass@localhost/mydb")


def test_dialect_uses_top_n():
    sql = TSqlDialect().limit("SELECT * FROM t WHERE x >= 0", 20)
    assert sql == "SELECT TOP 20 * FROM t WHERE x >= 0"


def test_dialect_float_ratio_uses_inherited_ansi_default():
    # T-SQL runs the portable `* 1.0` form unchanged; no override needed.
    assert TSqlDialect().float_ratio("n", "d") == "n * 1.0 / NULLIF(d, 0)"


def test_dialect_freshness_capability_is_column_only():
    assert TSqlDialect().freshness_sources == frozenset({"column"})


def test_dialect_introspection_capabilities_are_keys_and_stats():
    assert TSqlDialect().introspection_capabilities == frozenset({"keys", "stats"})


def test_null_rate_compiles_with_tsql_float_ratio():
    check = Check(source="s", object="dbo.fct", metric="null_rate", column="email")
    sql = compile_metric_sql(check, TSqlDialect())
    assert sql == (
        "SELECT SUM(CASE WHEN email IS NULL THEN 1 ELSE 0 END) * 1.0 "
        "/ NULLIF(COUNT(*), 0) FROM dbo.fct"
    )


def test_row_count_query_can_be_top_limited():
    check = Check(source="s", object="dbo.fct", metric="row_count")
    sql = compile_metric_sql(check, TSqlDialect())
    limited = TSqlDialect().limit(sql, 20)
    assert limited == "SELECT TOP 20 COUNT(*) FROM dbo.fct"


@pytest.mark.parametrize(
    ("type_name", "base_category", "expected"),
    [
        # MONEY/SMALLMONEY don't subclass sqlalchemy.types.Numeric, so the
        # base's generic isinstance mapping lands them in `other`; the
        # T-SQL-specific override corrects them to `numeric`.
        ("MONEY", Category.OTHER, Category.NUMERIC),
        ("SMALLMONEY", Category.OTHER, Category.NUMERIC),
        # Types the base already resolves correctly pass through unchanged.
        ("BIT", Category.BOOLEAN, Category.BOOLEAN),
        ("INT", Category.NUMERIC, Category.NUMERIC),
        ("DATETIME2", Category.TEMPORAL, Category.TEMPORAL),
        ("NVARCHAR(50)", Category.STRING, Category.STRING),
        # Genuinely unrecognized/unmapped native types land in `other`,
        # including T-SQL's binary rowversion TIMESTAMP (not a real
        # timestamp) and UNIQUEIDENTIFIER.
        ("TIMESTAMP", Category.OTHER, Category.OTHER),
        ("UNIQUEIDENTIFIER", Category.OTHER, Category.OTHER),
        ("SQL_VARIANT", Category.OTHER, Category.OTHER),
        ("GEOGRAPHY", Category.OTHER, Category.OTHER),
    ],
)
def test_refine_category(type_name, base_category, expected):
    assert refine_category(type_name, base_category) == expected


class _FakeInspector:
    """Stands in for SQLAlchemy's reflection Inspector in describe() tests."""

    def __init__(self, columns, pk_columns, uniques):
        self._columns = columns
        self._pk_columns = pk_columns
        self._uniques = uniques

    def get_columns(self, table, schema=None):
        return self._columns

    def get_pk_constraint(self, table, schema=None):
        return {"constrained_columns": self._pk_columns}

    def get_unique_constraints(self, table, schema=None):
        return self._uniques


def _make_bare_adapter() -> SqlServerAdapter:
    """A SqlServerAdapter with no live connection, for mocked-reflection tests."""
    adapter = SqlServerAdapter.__new__(SqlServerAdapter)
    adapter._conn = None
    adapter.dialect = TSqlDialect()
    return adapter


def test_describe_normalizes_mocked_reflection_and_row_count(monkeypatch):
    columns = [
        {"name": "id", "type": mssql.INTEGER(), "nullable": False},
        {"name": "amount", "type": mssql.MONEY(), "nullable": True},
        {"name": "seen_at", "type": mssql.DATETIME2(), "nullable": True},
        {"name": "notes", "type": mssql.NVARCHAR(200), "nullable": True},
        {"name": "row_id", "type": mssql.UNIQUEIDENTIFIER(), "nullable": False},
    ]
    fake_inspector = _FakeInspector(
        columns, pk_columns=["id"], uniques=[{"column_names": ["row_id"]}]
    )
    monkeypatch.setattr(
        "dbfresh.adapters.base.sqla_inspect", lambda conn: fake_inspector
    )

    adapter = _make_bare_adapter()
    monkeypatch.setattr(adapter, "scalar", lambda sql: 4200)

    info = adapter.describe("dbo.fct_sales")

    by_name = {c.name: c for c in info.columns}
    assert by_name["id"].category == Category.NUMERIC
    assert by_name["amount"].category == Category.NUMERIC
    assert by_name["amount"].type == "MONEY"
    assert by_name["seen_at"].category == Category.TEMPORAL
    assert by_name["notes"].category == Category.STRING
    assert by_name["notes"].nullable is True
    assert by_name["row_id"].category == Category.OTHER

    assert info.keys == [["id"], ["row_id"]]
    assert info.approx_row_count == 4200


def test_describe_row_count_is_none_when_object_has_no_partition_stats(monkeypatch):
    fake_inspector = _FakeInspector(
        [{"name": "id", "type": mssql.INTEGER(), "nullable": False}],
        pk_columns=[],
        uniques=[],
    )
    monkeypatch.setattr(
        "dbfresh.adapters.base.sqla_inspect", lambda conn: fake_inspector
    )

    adapter = _make_bare_adapter()
    monkeypatch.setattr(adapter, "scalar", lambda sql: None)

    info = adapter.describe("dbo.empty_or_missing")
    assert info.approx_row_count is None
    assert info.keys is None


def test_row_count_query_targets_partition_stats(monkeypatch):
    captured = {}

    def fake_scalar(sql):
        captured["sql"] = sql
        return 10

    monkeypatch.setattr(
        "dbfresh.adapters.base.sqla_inspect",
        lambda conn: _FakeInspector([], pk_columns=[], uniques=[]),
    )
    adapter = _make_bare_adapter()
    monkeypatch.setattr(adapter, "scalar", fake_scalar)

    adapter.describe("dbo.fct_sales")

    assert "sys.dm_db_partition_stats" in captured["sql"]
    assert "dbo.fct_sales" in captured["sql"]


@pytest.mark.skipif(
    not SQLSERVER_URL,
    reason="set DBFRESH_SQLSERVER_URL to run against a live SQL Server",
)
def test_live_describe_reflects_columns_keys_and_row_estimate():
    adapter = SqlServerAdapter(SQLSERVER_URL)
    try:
        adapter.rows(
            "IF OBJECT_ID('dbo.dbfresh_probe') IS NOT NULL DROP TABLE dbo.dbfresh_probe"
        )
        adapter.rows(
            "CREATE TABLE dbo.dbfresh_probe ("
            "id INT PRIMARY KEY, amount MONEY, seen_at DATETIME2)"
        )
        adapter.rows("INSERT INTO dbo.dbfresh_probe VALUES (1, 9.99, SYSDATETIME())")
        info = adapter.describe("dbo.dbfresh_probe")
        categories = {col.name: col.category for col in info.columns}
        assert categories["id"] == Category.NUMERIC
        assert categories["amount"] == Category.NUMERIC
        assert categories["seen_at"] == Category.TEMPORAL
        assert info.keys == [["id"]]
    finally:
        adapter.rows("DROP TABLE dbo.dbfresh_probe")
        adapter.close()
