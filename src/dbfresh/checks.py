"""Check model: durations, expectations, and SQL compilation."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from dbfresh.adapters.base import Column

_DURATION_TOKEN = re.compile(r"(\d+)([smhd])")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> timedelta:
    """Parse a compound duration such as ``26h``, ``90m``, or ``1h30m``.

    Supported units are ``s`` (seconds), ``m`` (minutes), ``h`` (hours), and
    ``d`` (days). The whole string must consist of one or more
    ``<integer><unit>`` tokens; anything else is a ``ValueError``.
    """
    stripped = text.strip()
    if not stripped:
        raise ValueError("empty duration")

    total = 0
    pos = 0
    for match in _DURATION_TOKEN.finditer(stripped):
        if match.start() != pos:
            raise ValueError(f"invalid duration: {text!r}")
        value, unit = match.groups()
        total += int(value) * _UNIT_SECONDS[unit]
        pos = match.end()

    if pos != len(stripped):
        raise ValueError(f"invalid duration: {text!r}")

    return timedelta(seconds=total)


_NUMERIC_OPERATORS = frozenset(
    {"between", "max", "lte", "min", "gte", "equals", "eq", "lt", "gt"}
)


@dataclass(frozen=True)
class Expectation:
    """A single-operator bound evaluated against an observed scalar."""

    operator: str
    operand: Any

    def evaluate(self, value: float | None) -> bool:
        if value is None:
            return False
        op, x = self.operator, self.operand
        if op == "between":
            lo, hi = x
            return lo <= value <= hi
        if op in ("max", "lte"):
            return value <= x
        if op in ("min", "gte"):
            return value >= x
        if op in ("equals", "eq"):
            return value == x
        if op == "lt":
            return value < x
        if op == "gt":
            return value > x
        if op == "max_lag":
            return value <= parse_duration(x).total_seconds()
        raise AssertionError(f"unhandled operator: {op!r}")

    def describe(self) -> str:
        if self.operator == "between":
            lo, hi = self.operand
            return f"between {lo} and {hi}"
        if self.operator == "unchanged":
            return "unchanged"
        if self.operator == "vs_previous":
            return _describe_vs_previous(self.operand)
        return f"{self.operator} {self.operand}"


def _describe_vs_previous(spec: dict) -> str:
    parts = [f"vs_previous({spec['baseline']})"]
    if spec["min_ratio"] is not None or spec["max_ratio"] is not None:
        lo = spec["min_ratio"] if spec["min_ratio"] is not None else "-"
        hi = spec["max_ratio"] if spec["max_ratio"] is not None else "-"
        parts.append(f"ratio [{lo}, {hi}]")
    if spec["min_delta"] is not None or spec["max_delta"] is not None:
        lo = spec["min_delta"] if spec["min_delta"] is not None else "-"
        hi = spec["max_delta"] if spec["max_delta"] is not None else "-"
        parts.append(f"delta [{lo}, {hi}]")
    return " ".join(parts)


_SCHEMA_OPERATORS = frozenset({"unchanged", "equals", "eq"})
_ALL_OPERATORS = _NUMERIC_OPERATORS | {"max_lag", "unchanged", "vs_previous"}
_VS_PREVIOUS_BASELINES = frozenset({"previous", "last_same_weekday"})
_ON_MISSING_MODES = frozenset({"pass", "warn", "skip"})


def _parse_vs_previous(operand: Any) -> dict:
    """Validate and normalize a ``vs_previous`` operand.

    Requires ``baseline`` (``previous`` | ``last_same_weekday``) and at
    least one guard (a ratio pair and/or a delta pair); ``on_missing``
    defaults to ``pass``.
    """
    if not isinstance(operand, dict):
        raise ValueError("'vs_previous' requires a mapping operand")
    baseline = operand.get("baseline")
    if baseline not in _VS_PREVIOUS_BASELINES:
        raise ValueError(
            "vs_previous.baseline must be one of "
            f"{sorted(_VS_PREVIOUS_BASELINES)}, got {baseline!r}"
        )
    on_missing = operand.get("on_missing", "pass")
    if on_missing not in _ON_MISSING_MODES:
        raise ValueError(
            f"vs_previous.on_missing must be one of {sorted(_ON_MISSING_MODES)}, "
            f"got {on_missing!r}"
        )
    min_ratio = operand.get("min_ratio")
    max_ratio = operand.get("max_ratio")
    min_delta = operand.get("min_delta")
    max_delta = operand.get("max_delta")
    if all(v is None for v in (min_ratio, max_ratio, min_delta, max_delta)):
        raise ValueError(
            "vs_previous requires at least one of "
            "min_ratio/max_ratio/min_delta/max_delta"
        )
    return {
        "baseline": baseline,
        "min_ratio": min_ratio,
        "max_ratio": max_ratio,
        "min_delta": min_delta,
        "max_delta": max_delta,
        "on_missing": on_missing,
    }


def parse_expectation(expect: dict, metric: str | None = None) -> Expectation:
    """Validate and build a single-operator :class:`Expectation`.

    Exactly one operator is allowed per check; ``{min, max}`` together is a
    validation error (use ``between``). When ``metric`` is given, operator
    compatibility is enforced: ``unchanged`` is valid only for a
    ``schema`` check, a ``schema`` check accepts only ``unchanged`` or
    ``equals``/``eq``, and ``vs_previous`` is rejected on ``freshness`` and
    ``schema`` (numeric metrics only).
    """
    if not expect:
        raise ValueError("expectation is empty")
    if len(expect) != 1:
        raise ValueError(
            f"a check takes exactly one expectation operator, got {sorted(expect)}"
        )
    [(operator, operand)] = expect.items()
    if operator not in _ALL_OPERATORS:
        raise ValueError(f"unknown or unsupported expectation operator: {operator!r}")
    if operator == "unchanged" and metric != "schema":
        raise ValueError("'unchanged' is only valid for the schema metric")
    if metric == "schema" and operator not in _SCHEMA_OPERATORS:
        raise ValueError(
            f"schema check does not support expectation operator: {operator!r}"
        )
    if operator == "vs_previous" and metric == "freshness":
        raise ValueError("'vs_previous' is not valid for a freshness check")
    if operator == "max_lag":
        parse_duration(operand)  # validate the duration up front
    elif operator == "between" and (
        not isinstance(operand, (list, tuple)) or len(operand) != 2
    ):
        raise ValueError("'between' requires exactly [lo, hi]")
    elif operator == "vs_previous":
        operand = _parse_vs_previous(operand)
    return Expectation(operator=operator, operand=operand)


@dataclass
class Check:
    """A single check definition."""

    source: str
    object: str
    metric: str | None = None
    column: str | None = None
    key: str | None = None
    where: str | None = None
    assert_: str | None = None
    expect: Expectation | None = None
    allow_empty: bool = False
    severity: str = "error"
    id: str | None = None
    by_weekday: dict[str, Expectation] | None = None
    on_holiday: Expectation | None = None
    calendar: str | None = None
    skip_off_schedule: bool = False
    freshness_source: str = "column"


_TABLE_LEVEL_METRICS = frozenset({"row_count", "schema"})
_WHITESPACE_RUN = re.compile(r"\s+")


def _normalize_assert_text(text: str) -> str:
    """Strip and collapse internal whitespace runs to a single space."""
    return _WHITESPACE_RUN.sub(" ", text.strip())


def check_id(check: Check) -> str:
    """A check's stable identity: the explicit ``id``, else a derived hash.

    The derived form hashes the identity tuple (source, object, metric, and
    the discriminating column/key, or the normalized assertion text) — never
    the expectation, so tuning a threshold preserves history.
    """
    if check.id:
        return check.id
    if check.assert_ is not None:
        metric = ""
        discriminant = _normalize_assert_text(check.assert_)
    else:
        metric = check.metric or ""
        if metric in _TABLE_LEVEL_METRICS:
            discriminant = ""
        else:
            discriminant = check.column or check.key or ""
    identity = "\x1f".join((check.source, check.object, metric, discriminant))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]


def describe_check(check: Check) -> str:
    """A short human-readable identity for error messages.

    Mirrors the discriminant :func:`check_id` hashes -- source, object,
    metric, and whichever of column/key applies (or the assertion text) --
    so a duplicate check_id error can name exactly what collided.
    """
    if check.assert_ is not None:
        return f"{check.source}.{check.object} assert {check.assert_!r}"
    metric = check.metric or "?"
    if check.column:
        return f"{check.source}.{check.object}/{metric} (column={check.column!r})"
    if check.key:
        return f"{check.source}.{check.object}/{metric} (key={check.key!r})"
    return f"{check.source}.{check.object}/{metric}"


def compile_metric_sql(check: Check, dialect: Any) -> str:
    """Compile a metric check to a single scalar-returning SQL query.

    Engine variances (float coercion, row capping) come from ``dialect``.
    """
    where = f" WHERE {check.where}" if check.where else ""
    if check.metric == "row_count":
        return f"SELECT COUNT(*) FROM {check.object}{where}"
    if check.metric == "null_rate":
        numerator = f"SUM(CASE WHEN {check.column} IS NULL THEN 1 ELSE 0 END)"
        ratio = dialect.float_ratio(numerator, "COUNT(*)")
        return f"SELECT {ratio} FROM {check.object}{where}"
    if check.metric == "duplicate_count":
        guard = f"WHERE {check.key} IS NOT NULL"
        if check.where:
            guard = f"{guard} AND {check.where}"
        return (
            f"SELECT COUNT(*) - COUNT(DISTINCT {check.key}) FROM {check.object} {guard}"
        )
    if check.metric in ("sum", "avg", "min", "max"):
        agg = check.metric.upper()
        return f"SELECT {agg}({check.column}) FROM {check.object}{where}"
    if check.metric == "freshness":
        return f"SELECT MAX({check.column}) FROM {check.object}{where}"
    raise ValueError(f"unsupported metric: {check.metric!r}")


def fingerprint_columns(columns: Iterable[Column]) -> str:
    """A stable serialization of a column set for the ``schema`` metric.

    Order-insensitive over ``(name, data_type)`` pairs; column order and
    nullability are excluded. Sorted, so identical column sets always
    serialize identically regardless of reflection order.
    """
    pairs = sorted((column.name, column.type) for column in columns)
    return "|".join(f"{name}:{data_type}" for name, data_type in pairs)


def _parse_fingerprint(fingerprint: str) -> dict[str, str]:
    """Parse a fingerprint string back into ``{name: data_type}``."""
    if not fingerprint:
        return {}
    pairs = (part.partition(":")[::2] for part in fingerprint.split("|"))
    return dict(pairs)


def diff_fingerprints(current: str, prior: str) -> list[str]:
    """Added / removed / retyped columns between two fingerprints.

    Each line is one of ``+ name (type)``, ``- name (type)``, or
    ``~ name (old_type -> new_type)``. Sorted by column name within each
    category so the result is deterministic.
    """
    current_cols = _parse_fingerprint(current)
    prior_cols = _parse_fingerprint(prior)
    lines = [
        f"+ {name} ({current_cols[name]})"
        for name in sorted(set(current_cols) - set(prior_cols))
    ]
    lines += [
        f"- {name} ({prior_cols[name]})"
        for name in sorted(set(prior_cols) - set(current_cols))
    ]
    lines += [
        f"~ {name} ({prior_cols[name]} -> {current_cols[name]})"
        for name in sorted(set(current_cols) & set(prior_cols))
        if current_cols[name] != prior_cols[name]
    ]
    return lines
