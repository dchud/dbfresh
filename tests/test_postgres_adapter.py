"""Contract-level tests for the reference PostgreSQL adapter.

No live PostgreSQL is required: the dialect and category-mapping tests are
pure. A live round-trip sits behind ``DBFRESH_PG_URL`` and is skipped unless
that env var is set.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import types as sqltypes
from sqlalchemy.engine import make_url

from dbfresh.adapters.base import Category
from dbfresh.adapters.postgres import PostgresAdapter, PostgresDialect, refine_category

PG_URL = os.environ.get("DBFRESH_PG_URL")


def test_module_imports_without_psycopg_installed():
    # psycopg is an optional extra, not a core dependency; this test
    # environment doesn't install it, so a bare import proves the module has
    # no module-level `import psycopg`. Constructing an adapter still needs
    # the driver -- see test_constructing_adapter_needs_psycopg below.
    import dbfresh.adapters.postgres as _  # noqa: F401


def test_constructing_adapter_needs_psycopg_only_at_connect_time():
    with pytest.raises(ModuleNotFoundError):
        PostgresAdapter(host="localhost")


def test_dialect_uses_limit():
    sql = PostgresDialect().limit("SELECT * FROM t", 20)
    assert sql == "SELECT * FROM t LIMIT 20"


def test_dialect_freshness_capability_is_column_only():
    assert PostgresDialect().freshness_sources == frozenset({"column"})


def test_dialect_introspection_capabilities_are_keys_only():
    # describe() no longer issues a reltuples row-count query, and the base
    # reflection it inherits never populates last_modified -- "stats" would
    # promise a field this adapter can no longer deliver.
    assert PostgresDialect().introspection_capabilities == frozenset({"keys"})


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


def _make_bare_adapter() -> PostgresAdapter:
    """A PostgresAdapter with no live connection, for mocked-reflection tests."""
    adapter = PostgresAdapter.__new__(PostgresAdapter)
    adapter._conn = None
    adapter.dialect = PostgresDialect()
    return adapter


def test_describe_does_not_issue_a_reltuples_query(monkeypatch):
    calls = []

    def fake_scalar(sql):
        calls.append(sql)
        return 10

    monkeypatch.setattr(
        "dbfresh.adapters.base.sqla_inspect",
        lambda conn: _FakeInspector(
            [{"name": "id", "type": sqltypes.INTEGER(), "nullable": False}],
            pk_columns=["id"],
            uniques=[],
        ),
    )
    adapter = _make_bare_adapter()
    monkeypatch.setattr(adapter, "scalar", fake_scalar)

    info = adapter.describe("fct")

    assert calls == []  # no per-run catalog query for an unused value
    assert info.keys == [["id"]]
    assert info.columns[0].category == Category.NUMERIC


@pytest.mark.parametrize(
    ("type_name", "base_category", "expected"),
    [
        # MONEY doesn't subclass sqlalchemy.types.Numeric, so the base's
        # generic isinstance mapping lands it in `other`; PostgreSQL's own
        # category override corrects it to `numeric`.
        ("MONEY", Category.OTHER, Category.NUMERIC),
        # Types the base already resolves correctly pass through unchanged.
        ("INTEGER", Category.NUMERIC, Category.NUMERIC),
        ("TIMESTAMP", Category.TEMPORAL, Category.TEMPORAL),
        ("VARCHAR", Category.STRING, Category.STRING),
        ("BOOLEAN", Category.BOOLEAN, Category.BOOLEAN),
        # Types with no override pass through whatever the base decided,
        # including `other` for genuinely unrecognized native types.
        ("JSONB", Category.OTHER, Category.OTHER),
        ("UUID", Category.OTHER, Category.OTHER),
    ],
)
def test_refine_category(type_name, base_category, expected):
    assert refine_category(type_name, base_category) == expected


@pytest.mark.skipif(
    not PG_URL, reason="set DBFRESH_PG_URL to run against a live PostgreSQL"
)
def test_live_describe_reflects_columns_and_keys():
    url = make_url(PG_URL)
    adapter = PostgresAdapter(
        host=url.host,
        port=url.port or 5432,
        database=url.database or "",
        user=url.username or "",
        password=url.password or "",
    )
    try:
        adapter.rows("DROP TABLE IF EXISTS dbfresh_e10_probe")
        adapter.rows(
            "CREATE TABLE dbfresh_e10_probe ("
            "id INTEGER PRIMARY KEY, amount MONEY, seen_at TIMESTAMP)"
        )
        adapter.rows("INSERT INTO dbfresh_e10_probe VALUES (1, 9.99, now())")
        info = adapter.describe("dbfresh_e10_probe")
        categories = {col.name: col.category for col in info.columns}
        assert categories["id"] == Category.NUMERIC
        assert categories["amount"] == Category.NUMERIC
        assert categories["seen_at"] == Category.TEMPORAL
        assert info.keys == [["id"]]
    finally:
        adapter.rows("DROP TABLE dbfresh_e10_probe")
        adapter.close()
