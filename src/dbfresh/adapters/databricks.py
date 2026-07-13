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


class DatabricksDialect(Dialect):
    name = "databricks"
    # Delta tables expose freshness via DESCRIBE metadata as well as a column.
    freshness_sources = frozenset({"column", "describe_history", "describe_detail"})
    # describe() populates only last_modified (a "stats" field): keys are
    # always None (Unity Catalog exposes no constraint metadata here).
    introspection_capabilities = frozenset({"stats"})


_HISTORY_DATA_OPERATIONS = frozenset({"WRITE", "MERGE", "DELETE", "UPDATE"})
# DESCRIBE HISTORY returns rows newest-first; recent operations are all
# that matter for a max-timestamp scan, so the fetch is bounded rather
# than pulling the table's full retained history.
_HISTORY_FETCH_LIMIT = 20


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
        # Declared as the base Dialect, not the inferred DatabricksDialect:
        # the Adapter protocol's `dialect` attribute is invariant, so a
        # narrower inferred type here would make DatabricksAdapter fail
        # structural matching against Adapter.
        self.dialect: Dialect = DatabricksDialect()

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

    def _rows_with_params(self, sql: str, parameters: dict[str, Any]) -> list[dict]:
        """Like :meth:`rows`, but with server-side bound parameters.

        Used only by the ``describe()`` metadata queries below, which embed
        a user-typed object name (the wizard/TUI feed exactly this path) --
        named paramstyle (``:name`` placeholders bound via ``parameters``)
        keeps a name containing a quote from ever landing in the SQL text,
        rather than the ``rows(sql)`` contract's plain string, which is fine
        for the compiler's own trusted-config SQL but not for this path.
        """
        cursor = self._conn.cursor()
        try:
            cursor.execute(sql, parameters)
            if cursor.description is None:
                return []
            columns = [d[0] for d in cursor.description]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
        finally:
            cursor.close()

    def describe(self, obj: str) -> ObjectInfo:
        """Hand-written object metadata: Unity Catalog reflection is thin.

        Columns come from ``information_schema.columns``; keys are always
        ``None`` (not exposed cheaply here); ``is_view`` comes from
        ``information_schema.tables`` -- it is what lets the freshness
        run-time guard reject ``describe_history``/``describe_detail``
        against a view. ``is_view`` is computed before ``last_modified``:
        DESCRIBE DETAIL describes Delta table storage, which a view has
        none of, so it is skipped entirely for a view rather than issued
        and made to fail.
        """
        columns = self._columns(obj)
        is_view = self._is_view(obj)
        last_modified = None if is_view else self._describe_detail_last_modified(obj)
        return ObjectInfo(
            columns=columns,
            keys=None,
            last_modified=last_modified,
            is_view=is_view,
        )

    def _columns(self, obj: str) -> list[Column]:
        catalog, schema, table = _split_qualified_name(obj)
        info_schema = (
            f"{catalog}.information_schema.columns"
            if catalog
            else "information_schema.columns"
        )
        where = ["table_name = :table_name"]
        params: dict[str, Any] = {"table_name": table}
        if schema:
            where.append("table_schema = :table_schema")
            params["table_schema"] = schema
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
            for row in self._rows_with_params(sql, params)
        ]

    def _is_view(self, obj: str) -> bool:
        """Whether ``obj`` is a view, from ``information_schema.tables``.

        ``table_type`` is ``'VIEW'`` for a view and something else (e.g.
        ``'MANAGED'``, ``'EXTERNAL'``) for a table. No matching catalog row
        -- the object doesn't exist -- defaults to ``False`` rather than
        erroring; a genuinely missing object is handled elsewhere.
        """
        catalog, schema, table = _split_qualified_name(obj)
        info_schema = (
            f"{catalog}.information_schema.tables"
            if catalog
            else "information_schema.tables"
        )
        where = ["table_name = :table_name"]
        params: dict[str, Any] = {"table_name": table}
        if schema:
            where.append("table_schema = :table_schema")
            params["table_schema"] = schema
        sql = f"SELECT table_type FROM {info_schema} WHERE {' AND '.join(where)}"
        result = self._rows_with_params(sql, params)
        if not result:
            return False
        return result[0]["table_type"] == "VIEW"

    def _describe_detail_last_modified(self, obj: str) -> datetime | None:
        """``lastModified`` from ``DESCRIBE DETAIL``, called only for a table.

        DESCRIBE DETAIL applies to Delta tables; called against a table in
        another format it can also fail. Any failure here degrades to
        ``None`` rather than raising the whole ``describe()`` call.
        """
        try:
            result = self.rows(f"DESCRIBE DETAIL {obj}")
        except Exception:
            return None
        if not result:
            return None
        return result[0].get("lastModified")

    def describe_history_last_modified(self, obj: str) -> datetime | None:
        """The most recent data-operation timestamp from ``DESCRIBE HISTORY``.

        Filtered to ``WRITE``/``MERGE``/``DELETE``/``UPDATE`` so
        ``OPTIMIZE``/``VACUUM`` maintenance noise never masks staleness.
        ``None`` when the table has no matching history entry (or none at
        all -- history retention is finite). The fetch is bounded to the
        most recent entries -- history retention can span the table's
        whole lifetime, and only recent operations matter for staleness.
        """
        history = self.rows(f"DESCRIBE HISTORY {obj} LIMIT {_HISTORY_FETCH_LIMIT}")
        timestamps = [
            row["timestamp"]
            for row in history
            if row.get("operation") in _HISTORY_DATA_OPERATIONS
        ]
        return max(timestamps) if timestamps else None

    def close(self) -> None:
        self._conn.close()
