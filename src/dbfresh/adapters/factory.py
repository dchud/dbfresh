"""Map a source type to its adapter. Adding an engine = one entry here."""

from __future__ import annotations

from typing import Any

from dbfresh.adapters.base import Dialect
from dbfresh.adapters.databricks import DatabricksAdapter, DatabricksDialect
from dbfresh.adapters.postgres import PostgresAdapter, PostgresDialect
from dbfresh.adapters.sqlite import SqliteAdapter, SqliteDialect
from dbfresh.adapters.sqlserver import SqlServerAdapter, TSqlDialect

_ADAPTERS = {
    "sqlite": SqliteAdapter,
    "postgres": PostgresAdapter,
    "sqlserver": SqlServerAdapter,
    "databricks": DatabricksAdapter,
}

_DIALECTS: dict[str, type[Dialect]] = {
    "sqlite": SqliteDialect,
    "postgres": PostgresDialect,
    "sqlserver": TSqlDialect,
    "databricks": DatabricksDialect,
}


def create_adapter(type_: str, params: dict[str, Any]):
    """Construct the adapter for a source ``type`` from its config params."""
    try:
        cls = _ADAPTERS[type_]
    except KeyError:
        raise ValueError(f"unknown source type: {type_!r}") from None
    return cls(**params)


def dialect_for_type(type_: str) -> Dialect:
    """The dialect for a source ``type``, instantiated with no connection.

    Every shipped ``Dialect`` subclass takes no constructor arguments, so
    this is safe to call at config-validation time -- before any source is
    reachable, and even when an engine's optional driver isn't installed.
    """
    try:
        cls = _DIALECTS[type_]
    except KeyError:
        raise ValueError(f"unknown source type: {type_!r}") from None
    return cls()
