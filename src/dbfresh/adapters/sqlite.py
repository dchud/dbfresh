"""SQLite adapter — the primary test engine, also usable as a real source."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from dbfresh.adapters.base import Dialect, SqlAlchemyAdapter


class SqliteDialect(Dialect):
    name = "sqlite"
    # describe() reflects primary/unique keys via the base's SQLAlchemy
    # Inspector; it never populates last_modified (no "stats" field).
    introspection_capabilities = frozenset({"keys"})


class SqliteAdapter(SqlAlchemyAdapter):
    """Adapter over a SQLite database. Defaults to an in-memory database.

    For the in-memory case a ``StaticPool`` keeps a single underlying
    connection so the database survives across queries, and
    ``check_same_thread=False`` lets it be used from a worker thread.
    """

    def __init__(self, database: str = ":memory:") -> None:
        if database in (":memory:", "", None):
            engine = create_engine(
                "sqlite://",
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
        else:
            engine = create_engine(
                f"sqlite:///{database}",
                connect_args={"check_same_thread": False},
            )
        super().__init__(engine, SqliteDialect())
