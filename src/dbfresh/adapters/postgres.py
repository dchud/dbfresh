"""PostgreSQL adapter — reference engine validating adapter extensibility.

PostgreSQL is not a supported v1 source; this module exists to prove the
claim that adding a new source engine is one module plus one factory
registration. ``scalar``, ``rows``, and the columns+keys of ``describe`` are
inherited from the SQLAlchemy-backed base's reflection; the only PostgreSQL-
specific pieces are a category-mapping refinement for native type names the
base doesn't already resolve, and a ``pg_class.reltuples`` row-count estimate.

``psycopg`` is an optional dependency (the ``postgres`` extra), not a core
runtime dependency. Nothing at module level imports it: ``create_engine``
only resolves and imports the driver when an adapter is actually constructed,
so this module imports fine even when ``psycopg`` isn't installed.
"""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from dbfresh.adapters.base import Category, Dialect, ObjectInfo, SqlAlchemyAdapter

# PostgreSQL-native type names that SQLAlchemy's generic isinstance checks
# (base.category_for) do not already resolve. MONEY is the one case: it does
# not subclass sqlalchemy.types.Numeric, so it lands in `other` by default.
# Every other PostgreSQL type reflects to a standard SQLAlchemy generic type
# (Integer, Numeric, DateTime, String, Boolean, ...) already covered there.
_CATEGORY_OVERRIDES: dict[str, Category] = {"MONEY": Category.NUMERIC}


def refine_category(type_name: str, base_category: Category) -> Category:
    """Apply PostgreSQL-specific overrides to a base-resolved category.

    ``type_name`` is the native type name as preserved on ``Column.type``
    (e.g. ``"MONEY"``, ``"INTEGER"``); ``base_category`` is what the base's
    generic SQLAlchemy-type mapping already produced for it. Names with no
    override pass ``base_category`` through unchanged.
    """
    return _CATEGORY_OVERRIDES.get(type_name, base_category)


class PostgresDialect(Dialect):
    """Reference PostgreSQL dialect: `LIMIT` row cap is inherited."""

    name = "postgres"
    freshness_sources = frozenset({"column"})
    introspection_capabilities = frozenset({"keys", "stats"})


class PostgresAdapter(SqlAlchemyAdapter):
    """Reference adapter over ``postgresql+psycopg://user:pass@host:port/db``.

    Not a supported v1 engine — this validates that a new source engine
    is cheap and additive.
    """

    def __init__(
        self,
        host: str,
        port: int = 5432,
        database: str = "",
        user: str = "",
        password: str = "",
    ) -> None:
        url = URL.create(
            "postgresql+psycopg",
            username=user or None,
            password=password or None,
            host=host,
            port=port,
            database=database or None,
        )
        engine = create_engine(url)
        super().__init__(engine, PostgresDialect())

    def describe(self, obj: str) -> ObjectInfo:
        """Reflect columns/keys via the base, then apply PG-specific extras.

        Refines each column's category (see ``refine_category``) and adds a
        ``pg_class.reltuples`` row-count estimate the base has no way to
        populate generically.
        """
        info = super().describe(obj)
        columns = [
            replace(col, category=refine_category(col.type, col.category))
            for col in info.columns
        ]
        return ObjectInfo(
            columns=columns,
            keys=info.keys,
            approx_row_count=self._reltuples_estimate(obj),
            last_modified=info.last_modified,
        )

    def _reltuples_estimate(self, obj: str) -> int | None:
        """A cheap, approximate row count from planner catalog statistics.

        ``reltuples`` is -1 for a relation that has never been analyzed, and
        the catalog lookup returns no row at all for an object that doesn't
        exist; both degrade to ``None`` rather than a misleading estimate.
        """
        reltuples = self.scalar(
            f"SELECT reltuples FROM pg_class WHERE oid = to_regclass('{obj}')"
        )
        if reltuples is None:
            return None
        count = int(reltuples)
        return count if count >= 0 else None
