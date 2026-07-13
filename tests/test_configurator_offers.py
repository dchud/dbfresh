"""Category -> offer mapping (spec section 11.2), the docs applicability
matrix's single source of truth."""

import pytest

from dbfresh.adapters.base import Category, Column
from dbfresh.configurator import (
    build_offered_check,
    category_offers,
    offered_column_checks,
)


def test_category_offers_numeric():
    assert category_offers(Category.NUMERIC) == [
        "null_rate",
        "sum",
        "avg",
        "min",
        "max",
        "duplicate_count",
    ]


def test_category_offers_temporal():
    assert category_offers(Category.TEMPORAL) == ["freshness", "null_rate"]


def test_category_offers_string():
    assert category_offers(Category.STRING) == ["null_rate", "duplicate_count"]


def test_category_offers_boolean():
    assert category_offers(Category.BOOLEAN) == ["null_rate"]


def test_category_offers_other():
    assert category_offers(Category.OTHER) == ["null_rate"]


def test_offered_column_checks_keys_off_category_not_native_type_name():
    # A column with a made-up native type string still offers numeric checks
    # -- offers key off `category` only, never the native type name.
    columns = [
        Column(
            name="weird", type="MADE_UP_TYPE", nullable=True, category=Category.NUMERIC
        )
    ]
    offers = offered_column_checks(columns)
    assert offers == [
        {
            "column": "weird",
            "category": "numeric",
            "checks": category_offers(Category.NUMERIC),
        }
    ]


def test_offered_column_checks_omits_null_rate_for_not_null_column():
    columns = [
        Column(name="id", type="INTEGER", nullable=False, category=Category.NUMERIC)
    ]
    offers = offered_column_checks(columns)
    assert "null_rate" not in offers[0]["checks"]
    assert "sum" in offers[0]["checks"]


def test_offered_column_checks_includes_null_rate_for_nullable_column():
    columns = [
        Column(name="email", type="TEXT", nullable=True, category=Category.STRING)
    ]
    offers = offered_column_checks(columns)
    assert offers[0]["checks"] == ["null_rate", "duplicate_count"]


def test_build_offered_check_null_rate_uses_given_max():
    block = build_offered_check(
        "s", "t", "email", "null_rate", False, max_null_rate=0.1
    )
    assert block == {
        "source": "s",
        "object": "t",
        "column": "email",
        "metric": "null_rate",
        "expect": {"max": 0.1},
    }


def test_build_offered_check_vs_previous_metrics_use_calendar_baseline():
    block = build_offered_check("s", "t", "amount", "sum", True)
    assert block["expect"]["vs_previous"]["baseline"] == "last_same_weekday"
    assert block["expect"]["vs_previous"]["min_ratio"] == 0.5
    assert block["expect"]["vs_previous"]["max_ratio"] == 2.0


def test_build_offered_check_vs_previous_metrics_default_baseline():
    block = build_offered_check("s", "t", "amount", "avg", False)
    assert block["expect"]["vs_previous"]["baseline"] == "previous"


def test_build_offered_check_duplicate_count_uses_column_as_key():
    block = build_offered_check("s", "t", "id", "duplicate_count", False)
    assert block["key"] == "id"
    assert "column" not in block
    assert block["expect"] == {"max": 0}


def test_build_offered_check_freshness_defaults_max_lag():
    block = build_offered_check("s", "t", "modified_at", "freshness", False)
    assert block["freshness_source"] == "column"
    assert block["expect"] == {"max_lag": "24h"}


def test_build_offered_check_freshness_honors_given_max_lag():
    block = build_offered_check(
        "s", "t", "modified_at", "freshness", False, max_lag="1h"
    )
    assert block["expect"] == {"max_lag": "1h"}


def test_build_offered_check_rejects_unsupported_metric():
    with pytest.raises(ValueError):
        build_offered_check("s", "t", "id", "bogus", False)
