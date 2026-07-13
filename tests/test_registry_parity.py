"""Parity tests keeping `registry.py` honest against the engine.

These never change engine or check-compiler behavior; they read the same
sets `checks.py` already defines (`_ALL_OPERATORS`) and probe
`compile_metric_sql` / `parse_expectation` directly, so a metric or operator
added to the code without a matching registry entry fails a test here
instead of silently drifting out of the generated docs.
"""

from __future__ import annotations

import inspect
import re

import pytest

from dbfresh import checks, registry
from dbfresh.adapters.base import Dialect
from dbfresh.checks import Check, compile_metric_sql, parse_expectation

# `schema` is dispatched directly in engine._evaluate_check via describe();
# unlike every other metric it never reaches compile_metric_sql.
_SCHEMA_METRIC = "schema"


def _minimal_check(metric: str) -> Check:
    """A Check with whatever discriminating field the metric requires filled in."""
    spec = registry.metric_by_name(metric)
    kwargs: dict = {"source": "s", "object": "t", "metric": metric}
    if spec.required == "column":
        kwargs["column"] = "c"
    elif spec.required == "key":
        kwargs["key"] = "k"
    return Check(**kwargs)


@pytest.mark.parametrize("spec", registry.METRICS, ids=lambda s: s.name)
def test_registry_metric_is_accepted_by_compile_metric_sql(spec):
    if spec.name == _SCHEMA_METRIC:
        pytest.skip("schema compiles via describe(), not compile_metric_sql")
    check = _minimal_check(spec.name)
    compile_metric_sql(check, Dialect())  # must not raise


def _metrics_compile_metric_sql_accepts() -> set[str]:
    """The metric-name literals `compile_metric_sql` actually branches on.

    Parses its own source for `check.metric == "..."` / `check.metric in
    (...)` comparisons rather than hand-listing them, so this test still
    catches a metric added to (or removed from) that dispatch even if the
    branches are reordered or renamed.
    """
    source = inspect.getsource(compile_metric_sql)
    names: set[str] = set()
    pattern = r'check\.metric\s*(?:==|in)\s*(\([^)]*\)|"[^"]*")'
    for match in re.finditer(pattern, source):
        names.update(re.findall(r'"([^"]*)"', match.group(1)))
    return names


def test_metric_registry_matches_compile_metric_sql_exactly():
    # `schema` never appears in compile_metric_sql (it dispatches via
    # describe() instead, see _SCHEMA_METRIC above) but is still a real,
    # registered metric, so it is unioned in on the code side too.
    code_metrics = _metrics_compile_metric_sql_accepts() | {_SCHEMA_METRIC}
    registry_metrics = {spec.name for spec in registry.METRICS}
    assert registry_metrics == code_metrics


def test_operator_registry_matches_all_operators_exactly():
    registry_operators = {spec.operator for spec in registry.OPERATORS}
    assert registry_operators == set(checks._ALL_OPERATORS)


# A valid (expect-dict, metric) fixture per operator, satisfying
# parse_expectation's per-metric compatibility rules: `unchanged`
# requires metric="schema"; `vs_previous` is rejected on "freshness"/"schema".
_OPERATOR_FIXTURES: dict[str, tuple[dict, str | None]] = {
    "between": ({"between": [0, 10]}, None),
    "max": ({"max": 10}, None),
    "lte": ({"lte": 10}, None),
    "min": ({"min": 0}, None),
    "gte": ({"gte": 0}, None),
    "equals": ({"equals": 5}, None),
    "eq": ({"eq": 5}, None),
    "lt": ({"lt": 10}, None),
    "gt": ({"gt": 0}, None),
    "max_lag": ({"max_lag": "1h"}, "freshness"),
    "vs_previous": (
        {"vs_previous": {"baseline": "previous", "min_ratio": 0.5, "max_ratio": 2.0}},
        "row_count",
    ),
    "unchanged": ({"unchanged": True}, "schema"),
}


@pytest.mark.parametrize("spec", registry.OPERATORS, ids=lambda s: s.operator)
def test_registry_operator_is_accepted_by_parse_expectation(spec):
    expect, metric = _OPERATOR_FIXTURES[spec.operator]
    parse_expectation(expect, metric=metric)  # must not raise


def test_every_operator_fixture_maps_to_a_registered_operator():
    """Guards the fixture table itself: no stray or missing operator keys."""
    assert set(_OPERATOR_FIXTURES) == {spec.operator for spec in registry.OPERATORS}
