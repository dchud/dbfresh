"""Authoritative metric and operator registries (§14 "single source of truth").

These are read-only descriptive catalogs, not new runtime behavior: they
exist so the documentation build (`dbfresh.docsgen`) can render the check,
expectation, and applicability reference tables from data instead of
hand-maintained prose. The check x data-type applicability matrix has no
separate registry here -- it reuses `configurator.category_offers`, which is
already the single source for that mapping (§11.2).

Parity tests (`tests/test_registry_parity.py`) assert every entry below is
actually accepted by the engine's compiler (`compile_metric_sql`) and
expectation parser (`parse_expectation`), and that the reverse holds too --
so a metric or operator can be added to the code and it will be caught here
if it is missing from the registry, and the docs alongside it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricSpec:
    """One row of the metric reference table (§6.2)."""

    name: str
    tier: str  # "table" | "column"
    required: str | None  # "column" | "key" | None
    description: str


METRICS: tuple[MetricSpec, ...] = (
    MetricSpec("row_count", "table", None, "Row count via `COUNT(*)`."),
    MetricSpec(
        "schema",
        "table",
        None,
        "Column-set fingerprint via `describe()`; compares `unchanged` or "
        "`equals` across runs.",
    ),
    MetricSpec(
        "null_rate", "column", "column", "Fraction of NULL values in one column."
    ),
    MetricSpec(
        "duplicate_count",
        "column",
        "key",
        "Rows sharing a duplicated, non-null key value.",
    ),
    MetricSpec("sum", "column", "column", "`SUM(column)`."),
    MetricSpec("avg", "column", "column", "`AVG(column)`."),
    MetricSpec("min", "column", "column", "`MIN(column)`."),
    MetricSpec("max", "column", "column", "`MAX(column)`."),
    MetricSpec(
        "freshness",
        "column",
        "column",
        "Lag since `MAX(column)`, or Delta DESCRIBE metadata on a Databricks "
        "table when `freshness_source` names it instead.",
    ),
)


def metric_by_name(name: str) -> MetricSpec:
    """The registered :class:`MetricSpec` for ``name``; raises if unknown."""
    for spec in METRICS:
        if spec.name == name:
            return spec
    raise KeyError(f"no registered metric named {name!r}")


@dataclass(frozen=True)
class OperatorSpec:
    """One row of the expectation-operator reference table (§6.3)."""

    operator: str
    meaning: str


OPERATORS: tuple[OperatorSpec, ...] = (
    OperatorSpec("between", "`lo <= v <= hi` (inclusive)"),
    OperatorSpec("max", "`v <= x`"),
    OperatorSpec("lte", "`v <= x` (alias of `max`)"),
    OperatorSpec("min", "`v >= x`"),
    OperatorSpec("gte", "`v >= x` (alias of `min`)"),
    OperatorSpec("equals", "`v == x`"),
    OperatorSpec("eq", "`v == x` (alias of `equals`)"),
    OperatorSpec("lt", "`v < x` (strict)"),
    OperatorSpec("gt", "`v > x` (strict)"),
    OperatorSpec("max_lag", "`now - max_ts <= duration` (freshness only)"),
    OperatorSpec(
        "vs_previous",
        "compares to a prior observation via ratio and/or delta guards "
        "(numeric metrics only; history-based, §8.3)",
    ),
    OperatorSpec(
        "unchanged",
        "the schema fingerprint equals the previous observation's (schema only)",
    ),
)


def operator_by_name(operator: str) -> OperatorSpec:
    """The registered :class:`OperatorSpec` for ``operator``; raises if unknown."""
    for spec in OPERATORS:
        if spec.operator == operator:
            return spec
    raise KeyError(f"no registered operator named {operator!r}")
