"""The status grids' day-column headers: the day name, and beneath it the
date -- month/day where the month could be in doubt (the first column, or
a change of month), the day number otherwise."""

import asyncio
from datetime import UTC, date, datetime

from textual.widgets import DataTable

from dbfresh.tui.app import DbfreshApp
from dbfresh.tui.dashboard import day_column_labels, trailing_dates
from dbfresh.tui.screens import ObjectDetailScreen


def test_first_column_has_month_and_the_rest_have_day_numbers():
    labels = day_column_labels(trailing_dates(date(2026, 7, 14)))
    assert labels == [
        ("Wed", "7/8"),
        ("Thu", "9"),
        ("Fri", "10"),
        ("Sat", "11"),
        ("Sun", "12"),
        ("Mon", "13"),
        ("Tue", "14"),
    ]


def test_a_change_of_month_shows_month_and_day():
    labels = day_column_labels(trailing_dates(date(2026, 10, 5)))
    assert [text for _, text in labels] == [
        "9/29",
        "30",
        "10/1",
        "2",
        "3",
        "4",
        "5",
    ]


def test_a_change_of_month_across_hidden_days_shows_month_and_day():
    # Sat 10/31 and Sun 11/1 hidden as empty non-business days.
    dates = [date(2026, 10, 29), date(2026, 10, 30), date(2026, 11, 2)]
    assert day_column_labels(dates) == [
        ("Thu", "10/29"),
        ("Fri", "30"),
        ("Mon", "11/2"),
    ]


def test_a_year_end_window():
    labels = day_column_labels(trailing_dates(date(2027, 1, 2)))
    assert [text for _, text in labels] == [
        "12/27",
        "28",
        "29",
        "30",
        "31",
        "1/1",
        "2",
    ]


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        frozen = datetime(2026, 10, 5, 12, tzinfo=UTC)
        return frozen.astimezone(tz) if tz is not None else frozen


def test_both_grids_show_the_date_under_each_day_name(tmp_path, monkeypatch):
    monkeypatch.setattr("dbfresh.tui.app.datetime", _FrozenDateTime)
    monkeypatch.setattr("dbfresh.tui.screens.datetime", _FrozenDateTime)
    monkeypatch.setattr("dbfresh.tui.app.display_timezone", lambda cal: UTC)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n"
        '  s: { type: sqlite, database: ":memory:" }\n'
        "checks:\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: row_count\n"
        "    expect: { min: 1 }\n"
    )
    app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))

    def day_headers(table):
        return [str(c.label) for c in list(table.columns.values())[2:]]

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            home = app.query_one("#dashboard-grid", DataTable)
            assert home.header_height == 2
            # Mon 10/5, no data: the weekend columns are hidden.
            assert day_headers(home) == [
                "Tue\n9/29",
                "Wed\n30",
                "Thu\n10/1",
                "Fri\n2",
                "Mon\n5",
            ]

            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ObjectDetailScreen)
            detail = app.screen.query_one(DataTable)
            assert detail.header_height == 2
            assert day_headers(detail) == day_headers(home)

    asyncio.run(scenario())
