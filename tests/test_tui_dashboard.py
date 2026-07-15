import asyncio
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from textual.app import App, ComposeResult
from textual.widgets import DataTable

from dbfresh.checks import Check, check_id
from dbfresh.config import Config
from dbfresh.engine import Result, Status
from dbfresh.store import Store
from dbfresh.tui.dashboard import (
    bucket_by_day,
    check_label,
    check_rows,
    object_rows,
    populate_grid,
    status_glyph,
    status_legend,
    status_style,
    trailing_dates,
)

_TODAY = date(2026, 7, 14)


def _checks():
    return [
        Check(source="s", object="orders", metric="row_count"),
        Check(source="s", object="orders", metric="schema"),
        Check(source="s", object="orders", metric="null_rate", column="email"),
        Check(source="s", object="orders", metric="freshness", column="modified_at"),
        Check(source="t", object="items", metric="duplicate_count", key="sku"),
    ]


def _config(checks):
    return Config(sources={}, checks=checks, config_dir=Path("."))


def _seed(store, check, status, observed_date, value=None):
    run_id = store.start_run()
    result = Result(
        object=check.object,
        metric=check.metric,
        status=status,
        source=check.source,
        value=value,
        check_id=check_id(check),
    )
    observed_at = datetime(
        observed_date.year, observed_date.month, observed_date.day, 12, tzinfo=UTC
    )
    store.record_observation(run_id, result, observed_at=observed_at)
    store.finish_run(run_id, status)


# -- trailing_dates -----------------------------------------------------


def test_trailing_dates_returns_seven_days_ending_today():
    dates = trailing_dates(_TODAY)
    assert len(dates) == 7
    assert dates[0] == date(2026, 7, 8)
    assert dates[-1] == _TODAY


def test_trailing_dates_respects_days_param():
    assert trailing_dates(_TODAY, days=3) == [
        date(2026, 7, 12),
        date(2026, 7, 13),
        date(2026, 7, 14),
    ]


# -- bucket_by_day --------------------------------------------------------


def test_bucket_by_day_maps_each_date_to_its_status():
    rows = [{"observed_at": "2026-07-14T09:00:00+00:00", "status": "OK"}]
    result = bucket_by_day(rows, trailing_dates(_TODAY), tz=None)
    assert result[_TODAY] == Status.OK


def test_bucket_by_day_date_with_no_observation_is_none():
    result = bucket_by_day([], trailing_dates(_TODAY), tz=None)
    assert all(status is None for status in result.values())


def test_bucket_by_day_multiple_runs_same_day_take_the_worst():
    rows = [
        {"observed_at": "2026-07-14T01:00:00+00:00", "status": "OK"},
        {"observed_at": "2026-07-14T09:00:00+00:00", "status": "FAIL"},
    ]
    result = bucket_by_day(rows, trailing_dates(_TODAY), tz=None)
    assert result[_TODAY] == Status.FAIL


def test_bucket_by_day_skipped_only_rolls_up_to_skipped_not_ok():
    rows = [{"observed_at": "2026-07-14T09:00:00+00:00", "status": "SKIPPED"}]
    result = bucket_by_day(rows, trailing_dates(_TODAY), tz=None)
    assert result[_TODAY] == Status.SKIPPED


def test_bucket_by_day_buckets_by_the_given_timezone():
    # Midnight UTC on the 14th is still the 13th in America/New_York
    # (-04:00 in July) -- bucketing must use the display timezone, not UTC.
    rows = [{"observed_at": "2026-07-14T00:30:00+00:00", "status": "OK"}]
    dates = trailing_dates(_TODAY)
    result_utc = bucket_by_day(rows, dates, tz=None)
    result_ny = bucket_by_day(rows, dates, tz=ZoneInfo("America/New_York"))
    assert result_utc[date(2026, 7, 14)] == Status.OK
    assert result_ny[date(2026, 7, 13)] == Status.OK
    assert result_ny[date(2026, 7, 14)] is None


# -- object_rows ------------------------------------------------------------


def test_object_rows_groups_by_source_then_object_sorted():
    store = Store(":memory:")
    rows = object_rows(_config(_checks()), store, _TODAY, tz=None)
    assert [(r.source, r.object) for r in rows] == [("s", "orders"), ("t", "items")]


def test_object_rows_label_is_source_dot_object():
    store = Store(":memory:")
    rows = object_rows(_config(_checks()), store, _TODAY, tz=None)
    assert rows[0].label == "s.orders"


def test_object_rows_with_no_observations_has_no_overall_or_days():
    store = Store(":memory:")
    rows = object_rows(_config(_checks()), store, _TODAY, tz=None)
    orders = rows[0]
    assert orders.overall is None
    assert all(status is None for status in orders.days)


def test_object_rows_overall_is_worst_of_the_objects_latest_checks():
    checks = _checks()
    store = Store(":memory:")
    _seed(store, checks[0], Status.OK, date(2026, 7, 14))  # row_count
    _seed(store, checks[2], Status.FAIL, date(2026, 7, 14))  # null_rate/email
    rows = object_rows(_config(checks), store, _TODAY, tz=None)
    orders = next(r for r in rows if r.object == "orders")
    assert orders.overall == Status.FAIL


def test_object_rows_day_column_rolls_up_across_the_objects_checks():
    checks = _checks()
    store = Store(":memory:")
    _seed(store, checks[0], Status.OK, date(2026, 7, 10))
    _seed(store, checks[2], Status.WARN, date(2026, 7, 10))
    rows = object_rows(_config(checks), store, _TODAY, tz=None)
    orders = next(r for r in rows if r.object == "orders")
    day_index = trailing_dates(_TODAY).index(date(2026, 7, 10))
    assert orders.days[day_index] == Status.WARN


def test_object_rows_carries_source_and_object_for_drill_in():
    store = Store(":memory:")
    rows = object_rows(_config(_checks()), store, _TODAY, tz=None)
    orders = next(r for r in rows if r.object == "orders")
    assert orders.source == "s"
    assert orders.object == "orders"
    assert orders.check is None


# -- check_rows ---------------------------------------------------------


def test_check_rows_returns_one_row_per_check_in_that_object():
    checks = _checks()
    store = Store(":memory:")
    rows = check_rows("s", "orders", _config(checks), store, _TODAY, tz=None)
    assert len(rows) == 4  # row_count, schema, null_rate, freshness -- not items/sku


def test_check_rows_label_disambiguates_column_or_key():
    checks = _checks()
    store = Store(":memory:")
    rows = check_rows("s", "orders", _config(checks), store, _TODAY, tz=None)
    labels = {r.label for r in rows}
    assert "null_rate (email)" in labels
    assert "freshness (modified_at)" in labels
    assert "row_count" in labels  # table-level: no parenthetical


def test_check_rows_row_carries_its_check_for_history_drill_in():
    checks = _checks()
    store = Store(":memory:")
    rows = check_rows("s", "orders", _config(checks), store, _TODAY, tz=None)
    row_count_row = next(r for r in rows if r.label == "row_count")
    assert row_count_row.check == checks[0]
    assert row_count_row.key == check_id(checks[0])


def test_check_rows_overall_reflects_that_checks_own_latest_status():
    checks = _checks()
    store = Store(":memory:")
    _seed(store, checks[0], Status.OK, date(2026, 7, 14))
    rows = check_rows("s", "orders", _config(checks), store, _TODAY, tz=None)
    row_count_row = next(r for r in rows if r.label == "row_count")
    schema_row = next(r for r in rows if r.label == "schema")
    assert row_count_row.overall == Status.OK
    assert schema_row.overall is None


# -- check_label ----------------------------------------------------------


def test_check_label_for_assert_():
    check = Check(source="s", object="orders", assert_="count(*) = 0")
    assert check_label(check) == "assert count(*) = 0"


def test_check_label_for_assert_sql():
    check = Check(source="s", object="orders", assert_sql="select 1 where 1=0")
    assert check_label(check) == "assert_sql select 1 where 1=0"


def test_check_label_falls_back_to_generic_check_only_without_metric_or_assert():
    check = Check(source="s", object="orders")
    assert check_label(check) == "check"


def test_check_label_table_level_metric_has_no_parenthetical():
    check = Check(source="s", object="orders", metric="row_count")
    assert check_label(check) == "row_count"


def test_check_label_column_level_metric_includes_column():
    check = Check(source="s", object="orders", metric="null_rate", column="email")
    assert check_label(check) == "null_rate (email)"


def test_check_label_key_level_metric_includes_key():
    check = Check(source="s", object="orders", metric="duplicate_count", key="sku")
    assert check_label(check) == "duplicate_count (sku)"


# -- status_glyph / status_style / status_legend ---------------------------


def test_status_glyph_fail_and_error_are_distinct():
    assert status_glyph(Status.FAIL) != status_glyph(Status.ERROR)


def test_status_style_fail_and_error_are_distinct():
    assert status_style(Status.FAIL) != status_style(Status.ERROR)


def test_status_glyph_skipped_and_never_observed_are_distinct():
    assert status_glyph(Status.SKIPPED) != status_glyph(None)


def test_status_style_skipped_and_never_observed_are_distinct():
    assert status_style(Status.SKIPPED) != status_style(None)


def test_status_legend_mentions_every_status_and_never_observed():
    legend = str(status_legend())
    for status in Status:
        assert status.value.lower() in legend.lower()
    assert "never observed" in legend


# -- populate_grid --------------------------------------------------------
#
# DataTable.add_column measures column width against self.app.console, so
# populate_grid can only run inside a mounted app -- these run through a
# bare test App via run_test(), same pattern test_tui_app.py uses for the
# full DbfreshApp.


class _GridTestApp(App):
    def compose(self) -> ComposeResult:
        yield DataTable(id="grid")


def test_populate_grid_builds_label_overall_and_seven_day_columns():
    async def scenario():
        store = Store(":memory:")
        rows = object_rows(_config(_checks()), store, _TODAY, tz=None)
        app = _GridTestApp()
        async with app.run_test():
            table = app.query_one(DataTable)
            populate_grid(table, rows, _TODAY, label_header="object")
            assert [str(c.label) for c in table.columns.values()] == [
                "object",
                "overall",
                "Wed",
                "Thu",
                "Fri",
                "Sat",
                "Sun",
                "Mon",
                "Tue",
            ]

    asyncio.run(scenario())


def test_populate_grid_one_row_per_grid_row():
    async def scenario():
        store = Store(":memory:")
        rows = object_rows(_config(_checks()), store, _TODAY, tz=None)
        app = _GridTestApp()
        async with app.run_test():
            table = app.query_one(DataTable)
            populate_grid(table, rows, _TODAY, label_header="object")
            assert table.row_count == len(rows)

    asyncio.run(scenario())


def test_populate_grid_rebuild_clears_previous_contents():
    async def scenario():
        store = Store(":memory:")
        rows = object_rows(_config(_checks()), store, _TODAY, tz=None)
        app = _GridTestApp()
        async with app.run_test():
            table = app.query_one(DataTable)
            populate_grid(table, rows, _TODAY, label_header="object")
            populate_grid(table, rows, _TODAY, label_header="object")
            assert table.row_count == len(rows)

    asyncio.run(scenario())


def test_populate_grid_row_key_matches_grid_row_key():
    async def scenario():
        store = Store(":memory:")
        rows = object_rows(_config(_checks()), store, _TODAY, tz=None)
        app = _GridTestApp()
        async with app.run_test():
            table = app.query_one(DataTable)
            populate_grid(table, rows, _TODAY, label_header="object")
            row_keys = {key.value for key in table.rows}
            assert row_keys == {row.key for row in rows}

    asyncio.run(scenario())
