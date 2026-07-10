"""SQL Server dialect (T-SQL). The pymssql-backed adapter is added separately."""

from __future__ import annotations

from dbfresh.adapters.base import Dialect


class TSqlDialect(Dialect):
    name = "tsql"

    def limit(self, sql: str, n: int) -> str:
        # T-SQL caps rows with TOP after SELECT, not a trailing LIMIT.
        return sql.replace("SELECT ", f"SELECT TOP {n} ", 1)
