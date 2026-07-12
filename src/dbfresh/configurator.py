"""Front-end-agnostic configurator: introspect, propose, emit YAML (§11).

All proposal, validation, YAML-serialization, connection-test, and
existence-check logic lives here as plain functions and dataclasses, so both
`dbfresh add` (a thin interactive shell) and the future TUI Configure screen
(E8) share one tested surface. This module never writes to the observation
store; it only reads catalog metadata via an adapter's ``describe()`` and
emits YAML for the version-controlled config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from dbfresh.adapters.base import Category, Column, Dialect, ObjectInfo

_ROW_COUNT_MIN_RATIO = 0.5
_ROW_COUNT_MAX_RATIO = 2.0
_DEFAULT_MAX_LAG = "24h"

_CONVENTIONAL_TIMESTAMP_NAMES = frozenset(
    {"modified_at", "updated_at", "loaded_at", "load_ts", "created_at"}
)
_CONVENTIONAL_TIMESTAMP_SUFFIXES = ("_at", "_ts", "_date")


@dataclass(frozen=True)
class TimestampChoice:
    """Result of the freshness timestamp-column heuristic (§11.1).

    ``column`` is set when a single unambiguous candidate was found.
    ``needs_choice`` is set instead when several temporal columns match and
    the wizard must ask rather than guess; ``candidates`` then lists them.
    """

    column: str | None = None
    needs_choice: bool = False
    candidates: list[str] = field(default_factory=list)


def _is_conventional_timestamp_name(name: str) -> bool:
    return name in _CONVENTIONAL_TIMESTAMP_NAMES or name.endswith(
        _CONVENTIONAL_TIMESTAMP_SUFFIXES
    )


def pick_timestamp_column(columns: list[Column]) -> TimestampChoice:
    """Auto-detect the freshness timestamp column among temporal columns.

    Prefers conventional names; if exactly one temporal column exists at
    all, uses it even when unconventionally named; otherwise several
    candidates match and the caller must ask the user to pick (§11.1).
    """
    temporal = [c for c in columns if c.category == Category.TEMPORAL]
    if not temporal:
        return TimestampChoice()

    conventional = [c for c in temporal if _is_conventional_timestamp_name(c.name)]
    if len(conventional) == 1:
        return TimestampChoice(column=conventional[0].name)
    if len(temporal) == 1:
        return TimestampChoice(column=temporal[0].name)

    pool = conventional or temporal
    return TimestampChoice(needs_choice=True, candidates=[c.name for c in pool])


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


def build_check(
    source: str,
    obj: str,
    metric: str,
    *,
    column: str | None = None,
    key: str | None = None,
    expect: dict,
    **extra: Any,
) -> dict:
    """Assemble one YAML-ready check block (§12.1 shape).

    The single builder used both by :func:`propose_checks` and by a wizard
    turning an offered column check (or a fully manual entry) into a block,
    so every emitted check has the same shape.
    """
    block: dict[str, Any] = {"source": source, "object": obj, "metric": metric}
    if column is not None:
        block["column"] = column
    if key is not None:
        block["key"] = key
    block.update(extra)
    block["expect"] = expect
    return block


def _row_count_baseline(has_calendar: bool) -> str:
    return "last_same_weekday" if has_calendar else "previous"


def propose_checks(
    source: str,
    obj: str,
    info: ObjectInfo,
    dialect: Dialect,
    has_calendar: bool = False,
    is_view: bool = False,
) -> list[dict]:
    """The metadata-driven proposal bundle for a named source + object (§11.1).

    Always proposes ``schema`` (unchanged) and a ``row_count`` volume-stability
    check. Proposes ``freshness`` on the auto-detected timestamp column
    (:func:`pick_timestamp_column`); when no column candidate exists, a
    Databricks-capable dialect on a table (not a view) falls back to
    ``describe_history``, otherwise no freshness check is proposed. Proposes
    one ``duplicate_count`` check per single-column key in ``info.keys``
    (composite keys are out of scope, §6.2).
    """
    checks: list[dict] = [
        build_check(source, obj, "schema", expect={"unchanged": True}),
        build_check(
            source,
            obj,
            "row_count",
            expect={
                "vs_previous": {
                    "baseline": _row_count_baseline(has_calendar),
                    "min_ratio": _ROW_COUNT_MIN_RATIO,
                    "max_ratio": _ROW_COUNT_MAX_RATIO,
                }
            },
        ),
    ]

    timestamp = pick_timestamp_column(info.columns)
    if timestamp.column is not None:
        checks.append(
            build_check(
                source,
                obj,
                "freshness",
                column=timestamp.column,
                freshness_source="column",
                expect={"max_lag": _DEFAULT_MAX_LAG},
            )
        )
    elif (
        not timestamp.needs_choice
        and not is_view
        and "describe_history" in dialect.freshness_sources
    ):
        checks.append(
            build_check(
                source,
                obj,
                "freshness",
                freshness_source="describe_history",
                expect={"max_lag": _DEFAULT_MAX_LAG},
            )
        )

    for key in info.keys or []:
        if len(key) == 1:
            checks.append(
                build_check(
                    source, obj, "duplicate_count", key=key[0], expect={"max": 0}
                )
            )

    return checks


@dataclass(frozen=True)
class ConnectionProbe:
    """Result of a mandatory connection test for a new source (§11.3)."""

    ok: bool
    error: str | None = None


def probe_connection(type_: str, params: dict) -> ConnectionProbe:
    """Build the adapter and run a trivial query to confirm it connects.

    Mandatory before writing a block for a brand-new source (§11.3); never
    raises -- any failure (unknown type, bad credentials, unreachable host)
    comes back as ``ConnectionProbe(ok=False, error=...)``.
    """
    from dbfresh.adapters.factory import create_adapter

    try:
        adapter = create_adapter(type_, params)
    except Exception as exc:
        return ConnectionProbe(ok=False, error=str(exc))
    try:
        adapter.scalar("SELECT 1")
    except Exception as exc:
        return ConnectionProbe(ok=False, error=str(exc))
    finally:
        adapter.close()
    return ConnectionProbe(ok=True)


@dataclass(frozen=True)
class ExistenceCheck:
    """Result of existence-checking a named object via ``describe()`` (§11.3).

    ``verified`` is ``False`` only when the source itself could not be
    reached (the caller passes ``adapter=None``), in which case ``exists``
    is ``None`` -- degraded manual entry, not a false negative. When
    ``verified`` is ``True``, ``exists`` reports whether ``describe()``
    succeeded, and ``info`` carries its result.
    """

    verified: bool
    exists: bool | None
    info: ObjectInfo | None = None
    error: str | None = None


def check_object_exists(adapter: Any | None, object_name: str) -> ExistenceCheck:
    """Existence-check ``object_name`` on ``adapter`` via ``describe()``.

    ``adapter`` is ``None`` when an already-configured source was found
    unreachable; the wizard degrades to manual entry and existence stays
    unverified rather than being reported as missing.
    """
    if adapter is None:
        return ExistenceCheck(verified=False, exists=None)
    try:
        info = adapter.describe(object_name)
    except Exception as exc:
        return ExistenceCheck(verified=True, exists=False, error=str(exc))
    return ExistenceCheck(verified=True, exists=True, info=info)
