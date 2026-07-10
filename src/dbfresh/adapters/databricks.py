"""Databricks dialect. The databricks-sql-connector adapter is added separately."""

from __future__ import annotations

from dbfresh.adapters.base import Dialect


class DatabricksDialect(Dialect):
    name = "databricks"
    # Delta tables expose freshness via DESCRIBE metadata as well as a column.
    freshness_sources = frozenset({"column", "describe_history", "describe_detail"})
