"""Freshness timestamp-column auto-detection heuristic."""

from dbfresh.adapters.base import Category, Column
from dbfresh.configurator import pick_timestamp_column


def _col(name, category=Category.TEMPORAL):
    return Column(name=name, type="TIMESTAMP", nullable=True, category=category)


def test_no_temporal_columns_yields_no_candidate():
    result = pick_timestamp_column([_col("id", Category.NUMERIC)])
    assert result.column is None
    assert result.needs_choice is False
    assert result.candidates == []


def test_single_temporal_column_is_used_even_if_unconventionally_named():
    result = pick_timestamp_column([_col("id", Category.NUMERIC), _col("dt1")])
    assert result.column == "dt1"
    assert result.needs_choice is False


def test_conventional_name_preferred_among_several_temporal_columns():
    result = pick_timestamp_column([_col("modified_at"), _col("dt1")])
    assert result.column == "modified_at"
    assert result.needs_choice is False


def test_all_listed_conventional_names_are_recognized():
    for name in ("modified_at", "updated_at", "loaded_at", "load_ts", "created_at"):
        result = pick_timestamp_column([_col(name), _col("dt1")])
        assert result.column == name


def test_suffix_variants_count_as_conventional():
    for name in ("last_seen_at", "sync_ts", "load_date"):
        result = pick_timestamp_column([_col(name), _col("dt1")])
        assert result.column == name


def test_multiple_conventional_matches_need_a_choice():
    result = pick_timestamp_column([_col("created_at"), _col("updated_at")])
    assert result.column is None
    assert result.needs_choice is True
    assert set(result.candidates) == {"created_at", "updated_at"}


def test_multiple_unconventional_temporal_columns_need_a_choice():
    result = pick_timestamp_column([_col("dt1"), _col("dt2")])
    assert result.column is None
    assert result.needs_choice is True
    assert set(result.candidates) == {"dt1", "dt2"}
