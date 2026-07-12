"""Front-end-agnostic configurator: introspect, propose, emit YAML (§11).

All proposal, validation, YAML-serialization, connection-test, and
existence-check logic lives here as plain functions and dataclasses, so both
`dbfresh add` (a thin interactive shell) and the future TUI Configure screen
(E8) share one tested surface. This module never writes to the observation
store; it only reads catalog metadata via an adapter's ``describe()`` and
emits YAML for the version-controlled config.
"""

from __future__ import annotations

from dbfresh.adapters.base import Category, Column

_CATEGORY_OFFERS: dict[Category, list[str]] = {
    Category.NUMERIC: ["null_rate", "sum", "avg", "min", "max", "duplicate_count"],
    Category.TEMPORAL: ["freshness", "null_rate"],
    Category.STRING: ["null_rate", "duplicate_count"],
    Category.BOOLEAN: ["null_rate"],
    Category.OTHER: ["null_rate"],
}


def category_offers(category: Category) -> list[str]:
    """Column-level checks offered for a category (§11.2).

    The single source of truth for the docs applicability matrix and for
    the wizard's per-column offer listing; keys off ``category`` only,
    never a native type name.
    """
    return list(_CATEGORY_OFFERS[category])


def offered_column_checks(columns: list[Column]) -> list[dict]:
    """Per-column offer entries: category-appropriate checks, not preselected.

    ``null_rate`` is omitted for ``NOT NULL`` columns -- the engine already
    enforces them (§11.1).
    """
    offers = []
    for column in columns:
        checks = [
            metric
            for metric in category_offers(column.category)
            if metric != "null_rate" or column.nullable
        ]
        offers.append(
            {"column": column.name, "category": column.category.value, "checks": checks}
        )
    return offers
