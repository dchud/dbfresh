from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
)

from dbfresh.adapters.base import Category, category_for


def test_numeric_types():
    assert category_for(Integer()) == Category.NUMERIC
    assert category_for(Float()) == Category.NUMERIC
    assert category_for(Numeric()) == Category.NUMERIC


def test_temporal_types():
    assert category_for(DateTime()) == Category.TEMPORAL
    assert category_for(Date()) == Category.TEMPORAL


def test_string_types():
    assert category_for(String()) == Category.STRING
    assert category_for(Text()) == Category.STRING


def test_boolean_type():
    assert category_for(Boolean()) == Category.BOOLEAN


def test_unknown_type_is_other():
    assert category_for(LargeBinary()) == Category.OTHER
