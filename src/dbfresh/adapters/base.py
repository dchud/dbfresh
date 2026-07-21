"""Adapter contract, canonical models, and the SQLAlchemy-backed base.

Every engine-specific adapter subclasses :class:`SqlAlchemyAdapter`, which
implements the contract methods over a SQLAlchemy ``Engine``. The adapter holds
a single connection for its lifetime — sources run one connection per worker
thread, never shared across threads. ``describe`` normalizes engine metadata via
SQLAlchemy reflection into the canonical :class:`ObjectInfo`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy import Engine, text
from sqlalchemy import inspect as sqla_inspect
from sqlalchemy import types as sqltypes


class Category(StrEnum):
    """Canonical column type-category vocabulary, shared across engines."""

    NUMERIC = "numeric"
    TEMPORAL = "temporal"
    STRING = "string"
    BOOLEAN = "boolean"
    OTHER = "other"


@dataclass(frozen=True)
class Column:
    """A column, with its native type name preserved for schema fingerprints."""

    name: str
    type: str
    nullable: bool
    category: Category


@dataclass(frozen=True)
class ObjectInfo:
    """Normalized object metadata. Optional fields are ``None`` when absent."""

    columns: list[Column]
    keys: list[list[str]] | None = None
    last_modified: datetime | None = None
    is_view: bool = False


def category_for(sqla_type: Any) -> Category:
    """Map a reflected SQLAlchemy type to a canonical :class:`Category`."""
    if isinstance(sqla_type, sqltypes.Boolean):
        return Category.BOOLEAN
    if isinstance(sqla_type, (sqltypes.Integer, sqltypes.Numeric)):
        return Category.NUMERIC
    if isinstance(
        sqla_type, (sqltypes.Date, sqltypes.DateTime, sqltypes.Time)
    ):
        return Category.TEMPORAL
    if isinstance(sqla_type, (sqltypes.String, sqltypes.Enum)):
        return Category.STRING
    return Category.OTHER


def _split_object(obj: str) -> tuple[str | None, str]:
    """Split ``obj`` into ``(schema, table)`` for ``Inspector`` reflection.

    Splits at the *last* dot, so a three-part name (``db.schema.table``,
    SQL Server's cross-database form) yields ``schema="db.schema"``, not
    ``("db", "schema", "table")``. That is not a bug for SQL Server: its
    SQLAlchemy dialect re-splits a dotted ``schema`` string itself (see
    ``sqlalchemy.dialects.mssql.base._schema_elements``), interpreting
    ``"db.schema"`` as database ``db`` / owner ``schema`` -- so the
    compound schema this returns reflects correctly end-to-end for T-SQL.
    Other SQLAlchemy-backed engines here (PostgreSQL, sqlite) have no such
    convention and only ever see two-part ``schema.table`` names in
    practice; passing a three-part name to one of them fails reflection
    outright rather than silently mapping to the wrong object.
    """
    if "." in obj:
        schema, _, table = obj.rpartition(".")
        return schema, table
    return None, obj


# freshness_source values that read Delta table metadata via DESCRIBE
# instead of querying a timestamp column -- Databricks-only, table-only
# (a view has no Delta storage for DESCRIBE to describe).
_DESCRIBE_FRESHNESS_SOURCES = frozenset(
    {"describe_history", "describe_detail"}
)


def validate_freshness_source(
    freshness_source: str, dialect: Dialect, is_view: bool = False
) -> None:
    """Validate a ``freshness_source`` against dialect capability and object kind.

    Raises ``ValueError`` when the dialect doesn't declare the source as a
    freshness capability, or when a metadata-based source
    (``describe_history``/``describe_detail``) is used against a view --
    those DESCRIBE forms describe table storage, which a view has none of;
    a view must use a timestamp ``column`` instead. Generic across every
    engine: the DESCRIBE forms happen to be Databricks-only today only
    because no other dialect declares them in ``freshness_sources``.
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


class Dialect:
    """SQL variances and capabilities for one engine family.

    The base implements the portable ANSI defaults; an engine's dialect
    overrides only what differs. The compiler asks the dialect for each
    variance — it never branches on an engine name.
    """

    name: str = "ansi"
    freshness_sources: frozenset[str] = frozenset({"column"})
    # What describe() can populate for this engine (e.g. "keys", "stats");
    # the base declares none so a bare Dialect() never raises when a caller
    # inspects it. Every shipped engine dialect overrides this.
    introspection_capabilities: frozenset[str] = frozenset()

    def limit(self, sql: str, n: int) -> str:
        """Cap a query's returned rows to ``n``."""
        return f"{sql} LIMIT {n}"

    def float_ratio(self, numerator: str, denominator: str) -> str:
        """Null-safe float division (portable ``* 1.0`` form)."""
        return f"{numerator} * 1.0 / NULLIF({denominator}, 0)"


class Adapter(Protocol):
    """The contract every source adapter implements: ``scalar``, ``rows``,
    ``describe``, ``close``, plus the ``dialect`` it compiles SQL for.

    Structural, not nominal: :class:`SqlAlchemyAdapter` subclasses and
    :class:`~dbfresh.adapters.databricks.DatabricksAdapter` (a native adapter
    with no shared base class) both satisfy it without inheriting from it.
    A Databricks-only capability like ``describe_history_last_modified`` is
    deliberately not part of this contract -- see
    :class:`HistoryAwareAdapter`.
    """

    dialect: Dialect

    def scalar(self, sql: str) -> Any: ...

    def rows(self, sql: str) -> list[dict[str, Any]]: ...

    def rows_limited(self, sql: str, n: int) -> list[dict[str, Any]]: ...

    def describe(self, obj: str) -> ObjectInfo: ...

    def close(self) -> None: ...


class HistoryAwareAdapter(Protocol):
    """Extension of :class:`Adapter` for engines with a DESCRIBE HISTORY-style
    freshness source -- currently Databricks only.

    Not merged into :class:`Adapter`: forcing every adapter to define
    ``describe_history_last_modified`` would mean stubbing it out on engines
    that have no such concept. The engine only reaches for it after
    ``validate_freshness_source`` has already confirmed the active dialect
    declares ``describe_history`` as a capability, so by the time it's
    called the adapter is guaranteed (at runtime) to provide it; the call
    site narrows via ``typing.cast`` rather than carrying the method on
    every adapter.
    """

    def describe_history_last_modified(self, obj: str) -> datetime | None: ...


class SqlAlchemyAdapter:
    """Base adapter over a SQLAlchemy :class:`~sqlalchemy.Engine`."""

    def __init__(self, engine: Engine, dialect: Dialect | None = None) -> None:
        self._engine = engine
        self._conn = engine.connect()
        self.dialect = dialect or Dialect()

    def scalar(self, sql: str) -> Any:
        """Run a query expected to return a single value."""
        return self._conn.execute(text(sql)).scalar()

    def rows(self, sql: str) -> list[dict]:
        """Run a query and return its rows as dicts.

        Statements that return no rows (DDL/DML) are committed and yield ``[]``.
        """
        result = self._conn.execute(text(sql))
        if not result.returns_rows:
            self._conn.commit()
            return []
        return [dict(row) for row in result.mappings().all()]

    def rows_limited(self, sql: str, n: int) -> list[dict]:
        """Run ``sql`` unmodified, fetching at most ``n`` rows via the cursor.

        Unlike :meth:`rows`, never caps via ``dialect.limit`` -- rewriting
        author-supplied SQL to inject a row cap can corrupt it (a cap
        injected inside a CTE truncates the scan instead of the returned
        rows; ``SELECT DISTINCT`` becomes invalid syntax under some
        dialects' rewrite). The query runs exactly as authored; the cap is
        applied client-side by fetching at most ``n`` rows off the cursor,
        leaving any further matching rows unconsumed.
        """
        result = self._conn.execute(text(sql))
        if not result.returns_rows:
            self._conn.commit()
            return []
        return [dict(row) for row in result.mappings().fetchmany(n)]

    def describe(self, obj: str) -> ObjectInfo:
        """Reflect columns, nullability, and key constraints into an ObjectInfo."""
        schema, table = _split_object(obj)
        insp = sqla_inspect(self._conn)
        columns = [
            Column(
                name=col["name"],
                type=str(col["type"]),
                nullable=bool(col["nullable"]),
                category=category_for(col["type"]),
            )
            for col in insp.get_columns(table, schema=schema)
        ]
        keys: list[list[str]] = []
        pk = insp.get_pk_constraint(table, schema=schema).get(
            "constrained_columns"
        )
        if pk:
            keys.append(list(pk))
        # SQL Server's SQLAlchemy dialect reflects columns and primary keys
        # but does not implement get_unique_constraints, so the base Dialect
        # method raises NotImplementedError. Unique constraints are
        # supplementary here -- the schema fingerprint reads columns only --
        # so degrade to primary-key-only rather than failing the whole
        # reflection (which would turn every schema check on such a source
        # into an ERROR).
        try:
            uniques = insp.get_unique_constraints(table, schema=schema)
        except NotImplementedError:
            uniques = []
        for unique in uniques:
            cols = unique.get("column_names")
            if cols:
                keys.append(list(cols))
        return ObjectInfo(columns=columns, keys=keys or None)

    def close(self) -> None:
        """Close the connection, then always dispose the engine.

        ``_conn.close()`` raising must not leak the engine's pool, so the
        dispose is unconditional; the original exception still propagates
        to the caller, which is expected to close every adapter under its
        own exception-safe guard (see ``dbfresh.runner.run_and_persist``).
        """
        try:
            self._conn.close()
        finally:
            self._engine.dispose()
