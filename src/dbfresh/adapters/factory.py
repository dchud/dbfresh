"""Map a source type to its adapter. Adding an engine = one entry here."""

from __future__ import annotations

from typing import Any

from dbfresh.adapters.postgres import PostgresAdapter
from dbfresh.adapters.sqlite import SqliteAdapter

_ADAPTERS = {"sqlite": SqliteAdapter, "postgres": PostgresAdapter}


def create_adapter(type_: str, params: dict[str, Any]):
    """Construct the adapter for a source ``type`` from its config params."""
    try:
        cls = _ADAPTERS[type_]
    except KeyError:
        raise ValueError(f"unknown source type: {type_!r}") from None
    return cls(**params)
