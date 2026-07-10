"""Check model: durations, expectations, and SQL compilation."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

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
        return f"{self.operator} {self.operand}"


def parse_expectation(expect: dict) -> Expectation:
    """Validate and build a single-operator :class:`Expectation`.

    Exactly one operator is allowed per check; ``{min, max}`` together is a
    validation error (use ``between``).
    """
    if not expect:
        raise ValueError("expectation is empty")
    if len(expect) != 1:
        raise ValueError(
            f"a check takes exactly one expectation operator, got {sorted(expect)}"
        )
    [(operator, operand)] = expect.items()
    if operator not in _NUMERIC_OPERATORS and operator != "max_lag":
        raise ValueError(f"unknown or unsupported expectation operator: {operator!r}")
    if operator == "max_lag":
        parse_duration(operand)  # validate the duration up front
    elif operator == "between" and (
        not isinstance(operand, (list, tuple)) or len(operand) != 2
    ):
        raise ValueError("'between' requires exactly [lo, hi]")
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


_TABLE_LEVEL_METRICS = frozenset({"row_count", "schema"})
_WHITESPACE_RUN = re.compile(r"\s+")


def _normalize_assert_text(text: str) -> str:
    """Strip and collapse internal whitespace runs to a single space."""
    return _WHITESPACE_RUN.sub(" ", text.strip())


def check_id(check: Check) -> str:
    """A check's stable identity: the explicit ``id``, else a derived hash.

    The derived form hashes the identity tuple (source, object, metric, and
    the discriminating column/key, or the normalized assertion text) — never
    the expectation, so tuning a threshold preserves history (§8.2).
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
