"""Databricks (Unity Catalog) adapter over ``databricks-sql-connector``.

Unlike the other engines, Databricks has no adequate SQLAlchemy dialect, so
this is a NATIVE adapter: it implements the four-method contract (``scalar``,
``rows``, ``describe``, ``close``) directly over a
``databricks.sql`` connection to a SQL warehouse, rather than subclassing the
SQLAlchemy-backed base. Unity Catalog reflection is thin, so ``describe`` is
hand-written: columns come from ``information_schema.columns``, keys are
always ``None`` (Unity Catalog exposes no constraint metadata here), and
``last_modified`` comes from ``DESCRIBE DETAIL``.

Freshness metadata has two origins beyond a trusted timestamp column,
both table-only (a view has no Delta storage to describe): ``DESCRIBE
HISTORY`` filtered to data operations (excludes ``OPTIMIZE``/``VACUUM``
noise), and ``DESCRIBE DETAIL``'s ``lastModified``. Never
``information_schema.tables``' last-altered column for data freshness --
it tracks DDL only, not inserts/updates/deletes.

``databricks-sql-connector`` is an optional dependency (the ``databricks``
extra), not a core runtime dependency. Nothing at module level imports it;
the import happens inside the adapter's constructor, so this module imports
fine even when the driver isn't installed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from dbfresh.adapters.base import Category, Column, Dialect, ObjectInfo

_NUMERIC_TYPES = frozenset(
    {"tinyint", "smallint", "int", "integer", "bigint", "float", "double", "decimal"}
)
_TEMPORAL_TYPES = frozenset({"date", "timestamp", "timestamp_ntz", "timestamp_ltz"})
_STRING_TYPES = frozenset({"string", "varchar", "char"})
_BOOLEAN_TYPES = frozenset({"boolean"})


def category_for_databricks(type_name: str) -> Category:
    """Map a Databricks/Spark SQL native type name to a canonical Category.

    Parametrized forms (``decimal(18,2)``, ``varchar(50)``) are matched on
    their base name. Unrecognized names -- including complex types
    (``array<...>``, ``map<...>``, ``struct<...>``) and ``variant`` --
    map to ``other``, never to an error.
    """
    base = type_name.split("(")[0].strip().lower()
    if base in _NUMERIC_TYPES:
        return Category.NUMERIC
    if base in _TEMPORAL_TYPES:
        return Category.TEMPORAL
    if base in _STRING_TYPES:
        return Category.STRING
    if base in _BOOLEAN_TYPES:
        return Category.BOOLEAN
    return Category.OTHER


_DESCRIBE_FRESHNESS_SOURCES = frozenset({"describe_history", "describe_detail"})


def validate_freshness_source(
    freshness_source: str, dialect: Dialect, is_view: bool = False
) -> None:
    """Validate a ``freshness_source`` against dialect capability and object kind.

    Raises ``ValueError`` when the dialect doesn't declare the source as a
    freshness capability, or when a metadata-based source
    (``describe_history``/``describe_detail``) is used against a view --
    those DESCRIBE forms describe table storage, which a view has none of;
    a view must use a timestamp ``column`` instead.
    """
    if freshness_source not in dialect.freshness_sources:
        raise ValueError(
            f"{dialect.name!r} dialect does not support "
            f"freshness_source {freshness_source!r}"
        )
    if freshness_source in _DESCRIBE_FRESHNESS_SOURCES and is_view:
        raise ValueError(
            f"freshness_source {freshness_source!r} is not valid for a view; "
            "views must use a timestamp column"
        )


class DatabricksDialect(Dialect):
    name = "databricks"
    # Delta tables expose freshness via DESCRIBE metadata as well as a column.
    freshness_sources = frozenset({"column", "describe_history", "describe_detail"})
    # describe() populates only last_modified (a "stats" field): keys are
    # always None and approx_row_count is never populated.
    introspection_capabilities = frozenset({"stats"})


_HISTORY_DATA_OPERATIONS = frozenset({"WRITE", "MERGE", "DELETE", "UPDATE"})


def _split_qualified_name(obj: str) -> tuple[str | None, str | None, str]:
    """Split a ``[catalog.][schema.]table`` object name into its parts."""
    parts = obj.split(".")
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return None, parts[0], parts[1]
    return None, None, parts[0]


class DatabricksAdapter:
    """Native adapter over a Databricks SQL warehouse connection.

    Config fields: ``host``, ``http_path`` (the warehouse endpoint), and
    ``token`` (a personal access token).
    """

    def __init__(self, host: str, http_path: str, token: str) -> None:
        import databricks.sql as dbsql

        self._conn = dbsql.connect(
            server_hostname=host, http_path=http_path, access_token=token
        )
        self.dialect = DatabricksDialect()

    def scalar(self, sql: str) -> Any:
        """Run a query expected to return a single value."""
        cursor = self._conn.cursor()
        try:
            cursor.execute(sql)
            row = cursor.fetchone()
            return row[0] if row is not None else None
        finally:
            cursor.close()

    def rows(self, sql: str) -> list[dict]:
        """Run a query and return its rows as dicts, keyed by column name."""
        cursor = self._conn.cursor()
        try:
            cursor.execute(sql)
            if cursor.description is None:
                return []
            columns = [d[0] for d in cursor.description]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
        finally:
            cursor.close()

    def describe(self, obj: str) -> ObjectInfo:
        """Hand-written object metadata: Unity Catalog reflection is thin.

        Columns come from ``information_schema.columns``; keys are always
        ``None`` and ``approx_row_count`` is never populated (neither is
        exposed cheaply here); ``last_modified`` comes from
        ``DESCRIBE DETAIL``.
        """
        return ObjectInfo(
            columns=self._columns(obj),
            keys=None,
            approx_row_count=None,
            last_modified=self._describe_detail_last_modified(obj),
        )

    def _columns(self, obj: str) -> list[Column]:
        catalog, schema, table = _split_qualified_name(obj)
        info_schema = (
            f"{catalog}.information_schema.columns"
            if catalog
            else "information_schema.columns"
        )
        where = [f"table_name = '{table}'"]
        if schema:
            where.append(f"table_schema = '{schema}'")
        sql = (
            f"SELECT column_name, data_type, is_nullable FROM {info_schema} "
            f"WHERE {' AND '.join(where)} ORDER BY ordinal_position"
        )
        return [
            Column(
                name=row["column_name"],
                type=row["data_type"],
                nullable=row["is_nullable"] == "YES",
                category=category_for_databricks(row["data_type"]),
            )
            for row in self.rows(sql)
        ]

    def _describe_detail_last_modified(self, obj: str) -> datetime | None:
        result = self.rows(f"DESCRIBE DETAIL {obj}")
        if not result:
            return None
        return result[0].get("lastModified")

    def describe_history_last_modified(self, obj: str) -> datetime | None:
        """The most recent data-operation timestamp from ``DESCRIBE HISTORY``.

        Filtered to ``WRITE``/``MERGE``/``DELETE``/``UPDATE`` so
        ``OPTIMIZE``/``VACUUM`` maintenance noise never masks staleness.
        ``None`` when the table has no matching history entry (or none at
        all -- history retention is finite).
        """
        history = self.rows(f"DESCRIBE HISTORY {obj}")
        timestamps = [
            row["timestamp"]
            for row in history
            if row.get("operation") in _HISTORY_DATA_OPERATIONS
        ]
        return max(timestamps) if timestamps else None

    def close(self) -> None:
        self._conn.close()
