import pytest

from dbfresh.checks import parse_expectation


def test_vs_previous_parses_with_baseline_previous_and_ratio_guards():
    e = parse_expectation(
        {
            "vs_previous": {
                "baseline": "previous",
                "min_ratio": 0.5,
                "max_ratio": 2.0,
            }
        },
        metric="row_count",
    )
    assert e.operator == "vs_previous"
    assert e.operand["baseline"] == "previous"
    assert e.operand["min_ratio"] == 0.5
    assert e.operand["max_ratio"] == 2.0


def test_vs_previous_defaults_on_missing_to_pass():
    e = parse_expectation(
        {"vs_previous": {"baseline": "previous", "min_ratio": 0.5, "max_ratio": 2.0}},
        metric="row_count",
    )
    assert e.operand["on_missing"] == "pass"


def test_vs_previous_accepts_explicit_on_missing():
    e = parse_expectation(
        {
            "vs_previous": {
                "baseline": "last_same_weekday",
                "min_ratio": 0.5,
                "max_ratio": 2.0,
                "on_missing": "warn",
            }
        },
        metric="row_count",
    )
    assert e.operand["on_missing"] == "warn"
    assert e.operand["baseline"] == "last_same_weekday"


def test_vs_previous_accepts_delta_guards_only():
    e = parse_expectation(
        {"vs_previous": {"baseline": "previous", "min_delta": -10, "max_delta": 10}},
        metric="row_count",
    )
    assert e.operand["min_delta"] == -10
    assert e.operand["max_delta"] == 10
    assert e.operand["min_ratio"] is None
    assert e.operand["max_ratio"] is None


def test_vs_previous_rejects_unknown_baseline():
    with pytest.raises(ValueError):
        parse_expectation(
            {"vs_previous": {"baseline": "bogus", "min_ratio": 0.5, "max_ratio": 2.0}},
            metric="row_count",
        )


def test_vs_previous_rejects_missing_baseline():
    with pytest.raises(ValueError):
        parse_expectation(
            {"vs_previous": {"min_ratio": 0.5, "max_ratio": 2.0}},
            metric="row_count",
        )


def test_vs_previous_rejects_unknown_on_missing():
    with pytest.raises(ValueError):
        parse_expectation(
            {
                "vs_previous": {
                    "baseline": "previous",
                    "min_ratio": 0.5,
                    "max_ratio": 2.0,
                    "on_missing": "bogus",
                }
            },
            metric="row_count",
        )


def test_vs_previous_requires_at_least_one_guard():
    with pytest.raises(ValueError):
        parse_expectation(
            {"vs_previous": {"baseline": "previous"}},
            metric="row_count",
        )


def test_vs_previous_rejected_for_freshness():
    with pytest.raises(ValueError):
        parse_expectation(
            {"vs_previous": {"baseline": "previous", "min_ratio": 0.5, "max_ratio": 2}},
            metric="freshness",
        )


def test_vs_previous_rejected_for_schema():
    with pytest.raises(ValueError):
        parse_expectation(
            {"vs_previous": {"baseline": "previous", "min_ratio": 0.5, "max_ratio": 2}},
            metric="schema",
        )


def test_vs_previous_rejected_combined_with_another_operator():
    with pytest.raises(ValueError):
        parse_expectation(
            {
                "vs_previous": {
                    "baseline": "previous",
                    "min_ratio": 0.5,
                    "max_ratio": 2.0,
                },
                "max": 100,
            },
            metric="row_count",
        )


def test_vs_previous_describe_mentions_baseline_and_ratio():
    e = parse_expectation(
        {"vs_previous": {"baseline": "previous", "min_ratio": 0.5, "max_ratio": 2.0}},
        metric="row_count",
    )
    described = e.describe()
    assert "previous" in described
    assert "0.5" in described
    assert "2.0" in described
