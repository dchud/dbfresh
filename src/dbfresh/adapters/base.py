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
from typing import Any

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
    approx_row_count: int | None = None
    last_modified: datetime | None = None


def category_for(sqla_type: Any) -> Category:
    """Map a reflected SQLAlchemy type to a canonical :class:`Category`."""
    if isinstance(sqla_type, sqltypes.Boolean):
        return Category.BOOLEAN
    if isinstance(sqla_type, (sqltypes.Integer, sqltypes.Numeric)):
        return Category.NUMERIC
    if isinstance(sqla_type, (sqltypes.Date, sqltypes.DateTime, sqltypes.Time)):
        return Category.TEMPORAL
    if isinstance(sqla_type, (sqltypes.String, sqltypes.Enum)):
        return Category.STRING
    return Category.OTHER


def _split_object(obj: str) -> tuple[str | None, str]:
    if "." in obj:
        schema, _, table = obj.rpartition(".")
        return schema, table
    return None, obj


class SqlAlchemyAdapter:
    """Base adapter over a SQLAlchemy :class:`~sqlalchemy.Engine`."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._conn = engine.connect()

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
        pk = insp.get_pk_constraint(table, schema=schema).get("constrained_columns")
        if pk:
            keys.append(list(pk))
        for unique in insp.get_unique_constraints(table, schema=schema):
            cols = unique.get("column_names")
            if cols:
                keys.append(list(cols))
        return ObjectInfo(columns=columns, keys=keys or None)

    def close(self) -> None:
        self._conn.close()
        self._engine.dispose()
