"""Map a source type to its adapter. Adding an engine = one entry here."""

from __future__ import annotations

import inspect
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


def adapter_class_for(type_: str) -> type:
    """The adapter class registered for ``type_``, without constructing it.

    Used by config validation to introspect a source's accepted
    constructor parameters (via ``inspect.signature``) before any
    connection is attempted.
    """
    try:
        return _ADAPTERS[type_]
    except KeyError:
        raise ValueError(f"unknown source type: {type_!r}") from None


def create_adapter(type_: str, params: dict[str, Any], timeout: int | None = None):
    """Construct the adapter for a source ``type`` from its config params.

    ``timeout`` (a source's connection timeout, §12.1) is forwarded to the
    adapter's constructor only when it declares a ``timeout`` parameter --
    engines that don't support one simply never receive it, with no
    engine-name test here.
    """
    cls = adapter_class_for(type_)
    kwargs = dict(params)
    if timeout is not None and "timeout" in inspect.signature(cls.__init__).parameters:
        kwargs["timeout"] = timeout
    return cls(**kwargs)


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
