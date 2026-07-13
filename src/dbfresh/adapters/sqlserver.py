"""SQL Server adapter (T-SQL) over ``mssql+pymssql://``.

``scalar``/``rows`` and the columns+keys half of ``describe`` are inherited
from the SQLAlchemy-backed base's reflection; the SQL-Server-specific pieces
are a category-mapping refinement for native type names the base doesn't
already resolve, and a ``sys.dm_db_partition_stats`` row-count estimate.

Config carries a single usql-style connection URL, kept in an environment
variable so credentials never appear in the checked-in YAML;
``connection.py`` parses and disambiguates it. ``pymssql`` is an optional
dependency (the ``sqlserver`` extra), not a core runtime dependency: nothing
at module level imports it, and ``create_engine`` only resolves the driver
when an adapter actually connects, so this module imports fine even when
``pymssql`` isn't installed.
"""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from dbfresh.adapters.base import Category, Dialect, ObjectInfo, SqlAlchemyAdapter
from dbfresh.connection import parse_sqlserver_url


class TSqlDialect(Dialect):
    name = "tsql"
    freshness_sources = frozenset({"column"})
    introspection_capabilities = frozenset({"keys", "stats"})

    def limit(self, sql: str, n: int) -> str:
        # T-SQL caps rows with TOP after SELECT, not a trailing LIMIT.
        return sql.replace("SELECT ", f"SELECT TOP {n} ", 1)


# T-SQL native type names that SQLAlchemy's generic isinstance checks
# (base.category_for) do not already resolve. MONEY and SMALLMONEY are the
# cases: neither subclasses sqlalchemy.types.Numeric, so they land in
# `other` by default. Every other T-SQL type reflects to a standard
# SQLAlchemy generic type (Integer, Numeric, DateTime, String, Boolean, ...)
# already covered there -- including T-SQL's binary rowversion `TIMESTAMP`
# type, which is correctly `other` (it is not a real timestamp).
_CATEGORY_OVERRIDES: dict[str, Category] = {
    "MONEY": Category.NUMERIC,
    "SMALLMONEY": Category.NUMERIC,
}


def refine_category(type_name: str, base_category: Category) -> Category:
    """Apply T-SQL-specific overrides to a base-resolved category.

    ``type_name`` is the native type name as preserved on ``Column.type``
    (e.g. ``"MONEY"``, ``"NVARCHAR(50)"``); ``base_category`` is what the
    base's generic SQLAlchemy-type mapping already produced for it. Names
    with no override pass ``base_category`` through unchanged.
    """
    return _CATEGORY_OVERRIDES.get(type_name, base_category)


class SqlServerAdapter(SqlAlchemyAdapter):
    """Adapter over ``mssql+pymssql://user:pass@host:port/database``.

    Takes a single usql-style connection URL (dbfresh.md 4.2): SQL
    authentication only, credentials inline, no Kerberos/ODBC setup needed.
    """

    def __init__(self, url: str, timeout: int | None = None) -> None:
        params = parse_sqlserver_url(url)
        engine_url = URL.create(
            "mssql+pymssql",
            username=params.user or None,
            password=params.password or None,
            host=params.server,
            port=params.port,
            database=params.database,
        )
        # pymssql's connection-attempt timeout is `login_timeout` (its
        # `timeout` kwarg instead caps query execution).
        connect_args = {"login_timeout": timeout} if timeout is not None else {}
        engine = create_engine(engine_url, connect_args=connect_args)
        super().__init__(engine, TSqlDialect())

    def describe(self, obj: str) -> ObjectInfo:
        """Reflect columns/keys via the base, then apply T-SQL-specific extras.

        Refines each column's category (see ``refine_category``) and adds a
        ``sys.dm_db_partition_stats`` row-count estimate the base has no
        way to populate generically.
        """
        info = super().describe(obj)
        columns = [
            replace(col, category=refine_category(col.type, col.category))
            for col in info.columns
        ]
        return ObjectInfo(
            columns=columns,
            keys=info.keys,
            approx_row_count=self._partition_stats_row_count(obj),
            last_modified=info.last_modified,
        )

    def _partition_stats_row_count(self, obj: str) -> int | None:
        """A cheap, approximate row count from partition catalog statistics.

        Sums ``row_count`` across the heap (``index_id = 0``) or clustered
        index (``index_id = 1``) partitions, whichever applies -- a table
        has exactly one of the two, so this never double-counts. An object
        with no matching partition stats (e.g. it doesn't exist) degrades
        to ``None`` rather than a misleading estimate.
        """
        count = self.scalar(
            "SELECT SUM(row_count) FROM sys.dm_db_partition_stats "
            f"WHERE object_id = OBJECT_ID('{obj}') AND index_id IN (0, 1)"
        )
        return None if count is None else int(count)
