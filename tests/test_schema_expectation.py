import pytest

from dbfresh.checks import parse_expectation


def test_unchanged_parses_for_schema_metric():
    e = parse_expectation({"unchanged": True}, metric="schema")
    assert e.operator == "unchanged"
    assert e.operand is True


def test_unchanged_rejected_without_metric_context():
    with pytest.raises(ValueError):
        parse_expectation({"unchanged": True})


def test_unchanged_rejected_for_non_schema_metric():
    with pytest.raises(ValueError):
        parse_expectation({"unchanged": True}, metric="row_count")


def test_schema_metric_accepts_equals():
    e = parse_expectation({"equals": "id:INTEGER"}, metric="schema")
    assert e.operator == "equals"
    assert e.operand == "id:INTEGER"


def test_schema_metric_accepts_eq_alias():
    e = parse_expectation({"eq": "id:INTEGER"}, metric="schema")
    assert e.operator == "eq"


def test_schema_metric_rejects_max():
    with pytest.raises(ValueError):
        parse_expectation({"max": 5}, metric="schema")


def test_schema_metric_rejects_between():
    with pytest.raises(ValueError):
        parse_expectation({"between": [1, 2]}, metric="schema")


def test_schema_metric_rejects_max_lag():
    with pytest.raises(ValueError):
        parse_expectation({"max_lag": "1h"}, metric="schema")


def test_schema_metric_rejects_vs_previous():
    with pytest.raises(ValueError):
        parse_expectation({"vs_previous": {"baseline": "previous"}}, metric="schema")


def test_unchanged_describe_is_concise():
    e = parse_expectation({"unchanged": True}, metric="schema")
    assert e.describe() == "unchanged"


def test_non_schema_metric_still_accepts_numeric_operators():
    e = parse_expectation({"max": 5}, metric="row_count")
    assert e.operator == "max"
