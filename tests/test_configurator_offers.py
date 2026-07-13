"""Category -> offer mapping (spec section 11.2), the docs applicability
matrix's single source of truth."""

from dbfresh.adapters.base import Category, Column
from dbfresh.configurator import category_offers, offered_column_checks


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
