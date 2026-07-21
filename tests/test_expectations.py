import pytest

from dbfresh.checks import parse_expectation


def test_between_is_inclusive():
    e = parse_expectation({"between": [10, 20]})
    assert e.evaluate(10) is True
    assert e.evaluate(20) is True
    assert e.evaluate(15) is True
    assert e.evaluate(9) is False
    assert e.evaluate(21) is False


def test_max_and_lte_alias():
    assert parse_expectation({"max": 5}).evaluate(5) is True
    assert parse_expectation({"max": 5}).evaluate(6) is False
    assert parse_expectation({"lte": 5}).evaluate(5) is True


def test_min_and_gte_alias():
    assert parse_expectation({"min": 5}).evaluate(5) is True
    assert parse_expectation({"min": 5}).evaluate(4) is False
    assert parse_expectation({"gte": 5}).evaluate(5) is True


def test_equals_and_eq_alias():
    assert parse_expectation({"equals": 3}).evaluate(3) is True
    assert parse_expectation({"equals": 3}).evaluate(4) is False
    assert parse_expectation({"eq": 3}).evaluate(3) is True


def test_lt_and_gt_are_strict():
    assert parse_expectation({"lt": 5}).evaluate(4) is True
    assert parse_expectation({"lt": 5}).evaluate(5) is False
    assert parse_expectation({"gt": 5}).evaluate(6) is True
    assert parse_expectation({"gt": 5}).evaluate(5) is False


def test_null_value_fails():
    assert parse_expectation({"max": 5}).evaluate(None) is False


def test_describe():
    assert parse_expectation({"max": 0.01}).describe() == "max 0.01"
    assert (
        parse_expectation({"between": [10, 20]}).describe()
        == "between 10 and 20"
    )


def test_empty_expectation_raises():
    with pytest.raises(ValueError):
        parse_expectation({})


def test_min_and_max_together_is_invalid():
    with pytest.raises(ValueError):
        parse_expectation({"min": 1, "max": 10})


def test_unknown_operator_raises():
    with pytest.raises(ValueError):
        parse_expectation({"bogus": 1})


def test_between_requires_two_values():
    with pytest.raises(ValueError):
        parse_expectation({"between": [10]})
