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
    GridRow,
    GridView,
    bucket_by_day,
    check_label,
    check_rows,
    header_key,
    is_header_key,
    last_run_line,
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
    observed_at = datetime(
        observed_date.year, observed_date.month, observed_date.day, 12, tzinfo=UTC
    )
    _seed_at(store, check, status, observed_at, value=value)


def _seed_at(store, check, status, observed_at, value=None):
    """Like :func:`_seed`, but at an exact timestamp rather than noon on a
    given date -- lets a test put more than one observation for the same
    check on the same calendar day, at different times, to exercise the
    day cell's latest-vs-worse-earlier-that-day marker."""
    run_id = store.start_run()
    result = Result(
        object=check.object,
        metric=check.metric,
        status=status,
        source=check.source,
        value=value,
        check_id=check_id(check),
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


def test_bucket_by_day_maps_each_date_to_its_latest_status():
    rows = [{"observed_at": "2026-07-14T09:00:00+00:00", "status": "OK"}]
    latest, all_statuses = bucket_by_day(rows, trailing_dates(_TODAY), tz=None)[_TODAY]
    assert latest == Status.OK
    assert all_statuses == [Status.OK]


def test_bucket_by_day_date_with_no_observation_is_none_and_empty():
    result = bucket_by_day([], trailing_dates(_TODAY), tz=None)
    assert all(entry == (None, []) for entry in result.values())


def test_bucket_by_day_same_day_takes_the_latest_not_the_worst():
    # The bug this fixes: a FAIL at 11:49 recovering to OK at 11:51 the
    # same day must read OK, not FAIL -- the worst status is still visible
    # in all_statuses, just not as the leading glyph.
    rows = [
        {"observed_at": "2026-07-14T11:49:00+00:00", "status": "FAIL"},
        {"observed_at": "2026-07-14T11:51:00+00:00", "status": "OK"},
    ]
    latest, all_statuses = bucket_by_day(rows, trailing_dates(_TODAY), tz=None)[_TODAY]
    assert latest == Status.OK
    assert set(all_statuses) == {Status.FAIL, Status.OK}


def test_bucket_by_day_latest_is_by_observed_at_not_row_order():
    # The later observation is listed first -- "latest" must still follow
    # observed_at, not input order.
    rows = [
        {"observed_at": "2026-07-14T11:51:00+00:00", "status": "OK"},
        {"observed_at": "2026-07-14T11:49:00+00:00", "status": "FAIL"},
    ]
    latest, _ = bucket_by_day(rows, trailing_dates(_TODAY), tz=None)[_TODAY]
    assert latest == Status.OK


def test_bucket_by_day_skipped_only_rolls_up_to_skipped_not_ok():
    rows = [{"observed_at": "2026-07-14T09:00:00+00:00", "status": "SKIPPED"}]
    latest, _ = bucket_by_day(rows, trailing_dates(_TODAY), tz=None)[_TODAY]
    assert latest == Status.SKIPPED


def test_bucket_by_day_buckets_by_the_given_timezone():
    # Midnight UTC on the 14th is still the 13th in America/New_York
    # (-04:00 in July) -- bucketing must use the display timezone, not UTC.
    rows = [{"observed_at": "2026-07-14T00:30:00+00:00", "status": "OK"}]
    dates = trailing_dates(_TODAY)
    result_utc = bucket_by_day(rows, dates, tz=None)
    result_ny = bucket_by_day(rows, dates, tz=ZoneInfo("America/New_York"))
    assert result_utc[date(2026, 7, 14)][0] == Status.OK
    assert result_ny[date(2026, 7, 13)][0] == Status.OK
    assert result_ny[date(2026, 7, 14)][0] is None


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
    assert all(day == (None, None) for day in orders.days)


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
    assert orders.days[day_index] == (Status.WARN, None)


def test_object_rows_day_marker_flags_a_same_day_recovery_on_one_check():
    # The bug this fixes, at the Home (multi-check) scope: null_rate FAILs
    # then recovers to OK later the same day -- the day cell should lead
    # with OK (matching overall) and carry a FAIL marker, not read FAIL.
    checks = _checks()
    store = Store(":memory:")
    _seed_at(store, checks[2], Status.FAIL, datetime(2026, 7, 10, 11, 49, tzinfo=UTC))
    _seed_at(store, checks[2], Status.OK, datetime(2026, 7, 10, 11, 51, tzinfo=UTC))
    rows = object_rows(_config(checks), store, _TODAY, tz=None)
    orders = next(r for r in rows if r.object == "orders")
    day_index = trailing_dates(_TODAY).index(date(2026, 7, 10))
    assert orders.days[day_index] == (Status.OK, Status.FAIL)


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


# -- day marker (single-check, drill-in scope) -----------------------------
#
# The drill-in (check_rows) is the degenerate single-check case of the same
# rollup the Home grid (object_rows) uses -- these exercise the marker rule
# itself without the extra multi-check indirection.

_DAY = date(2026, 7, 10)


def _row_count_day(store, checks):
    rows = check_rows("s", "orders", _config(checks), store, _TODAY, tz=None)
    row_count_row = next(r for r in rows if r.label == "row_count")
    day_index = trailing_dates(_TODAY).index(_DAY)
    return row_count_row.days[day_index]


def test_check_rows_day_marker_recovery_shows_latest_glyph_and_fail_marker():
    checks = _checks()
    store = Store(":memory:")
    _seed_at(store, checks[0], Status.FAIL, datetime(2026, 7, 10, 11, 49, tzinfo=UTC))
    _seed_at(store, checks[0], Status.OK, datetime(2026, 7, 10, 11, 51, tzinfo=UTC))
    assert _row_count_day(store, checks) == (Status.OK, Status.FAIL)


def test_check_rows_day_marker_fail_takes_priority_over_warn_and_error():
    checks = _checks()
    store = Store(":memory:")
    _seed_at(store, checks[0], Status.FAIL, datetime(2026, 7, 10, 9, 0, tzinfo=UTC))
    _seed_at(store, checks[0], Status.WARN, datetime(2026, 7, 10, 10, 0, tzinfo=UTC))
    _seed_at(store, checks[0], Status.ERROR, datetime(2026, 7, 10, 11, 0, tzinfo=UTC))
    _seed_at(store, checks[0], Status.OK, datetime(2026, 7, 10, 12, 0, tzinfo=UTC))
    assert _row_count_day(store, checks) == (Status.OK, Status.FAIL)


def test_check_rows_day_marker_warn_when_no_fail_present():
    checks = _checks()
    store = Store(":memory:")
    _seed_at(store, checks[0], Status.WARN, datetime(2026, 7, 10, 9, 0, tzinfo=UTC))
    _seed_at(store, checks[0], Status.OK, datetime(2026, 7, 10, 12, 0, tzinfo=UTC))
    assert _row_count_day(store, checks) == (Status.OK, Status.WARN)


def test_check_rows_day_marker_error_alone_still_marks_but_reads_neutral():
    # ERROR isn't a data failure (config mistake / unreachable source / a
    # comparison check's first run with no baseline) -- it still marks the
    # day (a worse status did happen), but callers render it as a neutral
    # dot rather than ERROR's own alarming blue glyph.
    checks = _checks()
    store = Store(":memory:")
    _seed_at(store, checks[0], Status.ERROR, datetime(2026, 7, 10, 9, 0, tzinfo=UTC))
    _seed_at(store, checks[0], Status.OK, datetime(2026, 7, 10, 12, 0, tzinfo=UTC))
    assert _row_count_day(store, checks) == (Status.OK, Status.ERROR)


def test_check_rows_day_marker_uniformly_ok_day_has_no_marker():
    checks = _checks()
    store = Store(":memory:")
    _seed_at(store, checks[0], Status.OK, datetime(2026, 7, 10, 9, 0, tzinfo=UTC))
    _seed_at(store, checks[0], Status.OK, datetime(2026, 7, 10, 12, 0, tzinfo=UTC))
    assert _row_count_day(store, checks) == (Status.OK, None)


def test_check_rows_day_marker_still_failing_day_has_no_marker():
    # Worst and latest are both FAIL -- nothing "worse" happened than
    # where the day ended up, so no marker, even though there were two
    # observations that day.
    checks = _checks()
    store = Store(":memory:")
    _seed_at(store, checks[0], Status.FAIL, datetime(2026, 7, 10, 9, 0, tzinfo=UTC))
    _seed_at(store, checks[0], Status.FAIL, datetime(2026, 7, 10, 12, 0, tzinfo=UTC))
    assert _row_count_day(store, checks) == (Status.FAIL, None)


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


# -- last_run_line ---------------------------------------------------------


def test_last_run_line_is_none_with_no_completed_run():
    store = Store(":memory:")
    assert last_run_line(store, tz=None) is None


def test_last_run_line_is_none_while_a_run_is_still_in_progress():
    store = Store(":memory:")
    store.start_run(started_at=datetime(2026, 7, 15, 14, 0, tzinfo=UTC))
    assert last_run_line(store, tz=None) is None


def test_last_run_line_reports_check_count_and_time():
    store = Store(":memory:")
    check = Check(source="s", object="orders", metric="row_count")
    run_id = store.start_run(started_at=datetime(2026, 7, 15, 14, 0, tzinfo=UTC))
    store.record_observation(
        run_id,
        Result(
            object="orders",
            metric="row_count",
            status=Status.OK,
            source="s",
            value=5,
            check_id=check_id(check),
        ),
    )
    store.finish_run(
        run_id, Status.OK, finished_at=datetime(2026, 7, 15, 14, 2, 30, tzinfo=UTC)
    )
    line = last_run_line(store, tz=UTC)
    assert line == "last run: 2026-07-15 14:02 · 1 checks · all ok"


def test_last_run_line_summarizes_non_ok_counts():
    store = Store(":memory:")
    run_id = store.start_run(started_at=datetime(2026, 7, 15, tzinfo=UTC))
    store.record_observation(
        run_id,
        Result(
            object="orders",
            metric="row_count",
            status=Status.OK,
            source="s",
            value=5,
            check_id="a",
        ),
    )
    store.record_observation(
        run_id,
        Result(
            object="orders",
            metric="null_rate",
            status=Status.FAIL,
            source="s",
            value=0.9,
            check_id="b",
        ),
    )
    store.finish_run(
        run_id, Status.FAIL, finished_at=datetime(2026, 7, 15, 14, 2, tzinfo=UTC)
    )
    line = last_run_line(store, tz=UTC)
    assert line == "last run: 2026-07-15 14:02 · 2 checks · 1 failed"


def test_last_run_line_reflects_that_runs_own_observations_not_current_status():
    """Counts come from the run's own observations, not each check's
    latest status -- a later, still-in-progress run must not change what
    the last *completed* run's line reports."""
    store = Store(":memory:")
    check = Check(source="s", object="orders", metric="row_count")
    first = store.start_run(started_at=datetime(2026, 7, 15, tzinfo=UTC))
    store.record_observation(
        first,
        Result(
            object="orders",
            metric="row_count",
            status=Status.OK,
            source="s",
            value=5,
            check_id=check_id(check),
        ),
    )
    store.finish_run(
        first, Status.OK, finished_at=datetime(2026, 7, 15, 14, 2, tzinfo=UTC)
    )
    store.start_run(started_at=datetime(2026, 7, 15, 15, 0, tzinfo=UTC))  # unfinished

    line = last_run_line(store, tz=UTC)
    assert line == "last run: 2026-07-15 14:02 · 1 checks · all ok"


def test_last_run_line_uses_the_given_display_timezone():
    store = Store(":memory:")
    run_id = store.start_run(started_at=datetime(2026, 7, 15, tzinfo=UTC))
    store.finish_run(
        run_id, Status.OK, finished_at=datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
    )
    line = last_run_line(store, tz=ZoneInfo("America/New_York"))
    assert line is not None
    assert "10:00" in line  # 14:00 UTC == 10:00 EDT in July


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


def test_populate_grid_day_cell_renders_latest_glyph_plus_marker():
    async def scenario():
        checks = _checks()
        store = Store(":memory:")
        _seed_at(
            store, checks[2], Status.FAIL, datetime(2026, 7, 10, 11, 49, tzinfo=UTC)
        )
        _seed_at(store, checks[2], Status.OK, datetime(2026, 7, 10, 11, 51, tzinfo=UTC))
        rows = object_rows(_config(checks), store, _TODAY, tz=None)
        app = _GridTestApp()
        async with app.run_test():
            table = app.query_one(DataTable)
            populate_grid(table, rows, _TODAY, label_header="object")
            orders_key = next(r for r in rows if r.object == "orders").key
            marked_day = date(2026, 7, 10).isoformat()
            cell = table.get_cell(orders_key, marked_day)
            assert cell.plain == " ✓✗"  # centered glyph, marker to its right

    asyncio.run(scenario())


def test_populate_grid_day_cell_with_no_marker_is_a_single_glyph():
    async def scenario():
        store = Store(":memory:")
        rows = object_rows(_config(_checks()), store, _TODAY, tz=None)
        app = _GridTestApp()
        async with app.run_test():
            table = app.query_one(DataTable)
            populate_grid(table, rows, _TODAY, label_header="object")
            orders_key = next(r for r in rows if r.object == "orders").key
            cell = table.get_cell(orders_key, _TODAY.isoformat())
            assert cell.plain == "·"  # never observed, no marker to append

    asyncio.run(scenario())


def test_populate_grid_overall_column_never_carries_a_marker():
    async def scenario():
        checks = _checks()
        store = Store(":memory:")
        _seed_at(
            store, checks[2], Status.FAIL, datetime(2026, 7, 10, 11, 49, tzinfo=UTC)
        )
        _seed_at(store, checks[2], Status.OK, datetime(2026, 7, 10, 11, 51, tzinfo=UTC))
        rows = object_rows(_config(checks), store, _TODAY, tz=None)
        app = _GridTestApp()
        async with app.run_test():
            table = app.query_one(DataTable)
            populate_grid(table, rows, _TODAY, label_header="object")
            orders_key = next(r for r in rows if r.object == "orders").key
            cell = table.get_cell(orders_key, "overall")
            assert cell.plain == "✓"  # single glyph -- overall never marks

    asyncio.run(scenario())


def test_populate_grid_ungrouped_label_is_full_source_dot_object():
    # group_headers off (the default, the drill-in scope) -- unchanged from
    # before grouping existed: the label cell holds the row's own row.label.
    async def scenario():
        store = Store(":memory:")
        rows = object_rows(_config(_checks()), store, _TODAY, tz=None)
        app = _GridTestApp()
        async with app.run_test():
            table = app.query_one(DataTable)
            populate_grid(table, rows, _TODAY, label_header="object")
            orders_key = next(r for r in rows if r.object == "orders").key
            assert table.get_cell(orders_key, "label") == "s.orders"

    asyncio.run(scenario())


# -- populate_grid(group_headers=True) -- the Home grid's source grouping --


def test_populate_grid_grouped_inserts_one_header_row_per_source():
    async def scenario():
        store = Store(":memory:")
        rows = object_rows(_config(_checks()), store, _TODAY, tz=None)  # sources s, t
        app = _GridTestApp()
        async with app.run_test():
            table = app.query_one(DataTable)
            populate_grid(
                table, rows, _TODAY, label_header="object", group_headers=True
            )
            assert table.row_count == len(rows) + 2  # one header per source
            row_keys = [key.value for key in table.rows]
            assert row_keys == [
                header_key("s"),
                "s\x1forders",
                header_key("t"),
                "t\x1fitems",
            ]

    asyncio.run(scenario())


def test_populate_grid_grouped_object_row_label_is_object_only():
    async def scenario():
        store = Store(":memory:")
        rows = object_rows(_config(_checks()), store, _TODAY, tz=None)
        app = _GridTestApp()
        async with app.run_test():
            table = app.query_one(DataTable)
            populate_grid(
                table, rows, _TODAY, label_header="object", group_headers=True
            )
            # Not "s.orders" -- the header row above it already names the
            # source.
            assert table.get_cell("s\x1forders", "label") == "orders"

    asyncio.run(scenario())


def test_populate_grid_grouped_header_row_label_is_bold_source_name():
    async def scenario():
        store = Store(":memory:")
        rows = object_rows(_config(_checks()), store, _TODAY, tz=None)
        app = _GridTestApp()
        async with app.run_test():
            table = app.query_one(DataTable)
            populate_grid(
                table, rows, _TODAY, label_header="object", group_headers=True
            )
            cell = table.get_cell(header_key("s"), "label")
            assert cell.plain == "s"
            assert cell.style == "bold"

    asyncio.run(scenario())


def test_populate_grid_grouped_header_row_overall_and_day_cells_are_blank():
    async def scenario():
        store = Store(":memory:")
        rows = object_rows(_config(_checks()), store, _TODAY, tz=None)
        app = _GridTestApp()
        async with app.run_test():
            table = app.query_one(DataTable)
            populate_grid(
                table, rows, _TODAY, label_header="object", group_headers=True
            )
            assert table.get_cell(header_key("s"), "overall") == ""
            assert table.get_cell(header_key("s"), _TODAY.isoformat()) == ""

    asyncio.run(scenario())


# -- header_key / is_header_key ---------------------------------------------


def test_header_key_is_distinct_from_a_real_object_row_key():
    assert header_key("s") != "s\x1forders"


def test_is_header_key_true_only_for_a_header_key():
    assert is_header_key(header_key("s"))
    assert not is_header_key("s\x1forders")
    assert not is_header_key(None)


# -- GridView (Home grid filter/search) --------------------------------------


def _view_row(label: str, overall: Status | None) -> GridRow:
    source, obj = label.split(".", 1)
    return GridRow(key=f"{source}\x1f{obj}", label=label, overall=overall, days=[])


def _view_rows() -> list[GridRow]:
    return [
        _view_row("s.a", Status.OK),
        _view_row("s.b", Status.WARN),
        _view_row("s.c", Status.FAIL),
        _view_row("t.d", Status.ERROR),
        _view_row("t.e", Status.SKIPPED),
        _view_row("t.f", None),
    ]


def test_grid_view_default_returns_rows_unchanged():
    rows = _view_rows()
    assert GridView().apply(rows) == rows


def test_grid_view_default_is_not_active():
    assert not GridView().active


def test_grid_view_hide_ok_drops_only_ok_rows():
    rows = _view_rows()
    visible = GridView(hide_ok=True).apply(rows)
    assert [r.label for r in visible] == ["s.b", "s.c", "t.d", "t.e", "t.f"]


def test_grid_view_search_matches_label_substring_case_insensitively():
    rows = [_view_row("s.orders", Status.OK), _view_row("s.items", Status.OK)]
    visible = GridView(search="ORD").apply(rows)
    assert [r.label for r in visible] == ["s.orders"]


def test_grid_view_search_whitespace_only_is_treated_as_no_search():
    rows = _view_rows()
    assert GridView(search="   ").apply(rows) == rows
    assert not GridView(search="   ").active


def test_grid_view_search_matching_nothing_returns_empty():
    rows = _view_rows()
    assert GridView(search="nonexistent").apply(rows) == []


def test_grid_view_never_reorders_the_incoming_source_object_order():
    # populate_grid's grouping depends on rows staying in (source, object)
    # order -- filtering must only ever narrow that order, never reorder it.
    rows = _view_rows()
    visible = GridView(hide_ok=True).apply(rows)
    assert visible == [row for row in rows if row.overall != Status.OK]


def test_grid_view_combines_filter_and_search():
    rows = [
        _view_row("s.orders", Status.OK),
        _view_row("s.order_items", Status.ERROR),
        _view_row("s.other", Status.WARN),
    ]
    view = GridView(hide_ok=True, search="order")
    visible = view.apply(rows)
    assert [r.label for r in visible] == ["s.order_items"]


def test_grid_view_active_true_when_any_control_is_set():
    assert GridView(hide_ok=True).active
    assert GridView(search="x").active


def test_grid_view_status_text_empty_when_inactive():
    assert GridView().status_text() == ""


def test_grid_view_status_text_lists_each_active_control():
    text = GridView(hide_ok=True, search="abc").status_text()
    assert "non-OK only" in text
    assert "abc" in text


def test_grid_view_has_no_worst_first_field():
    # Grouping (populate_grid's group_headers) is incompatible with a
    # global severity sort -- retired in favor of the non-OK filter ('f')
    # for triage.
    assert "worst_first" not in GridView.__dataclass_fields__
