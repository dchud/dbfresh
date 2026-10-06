"""The TUI status grids drop a trailing-day column that carries nothing --
not today, not a business day, and no observation in any row -- so a
deployment that only runs on business days doesn't see two empty weekend
columns. A day with data is always shown, so this never hides anything."""

import asyncio
from datetime import UTC, date, datetime

from textual.app import App, ComposeResult
from textual.widgets import DataTable

from dbfresh.calendar import build_calendar
from dbfresh.checks import check_id
from dbfresh.config import load_config
from dbfresh.engine import Result, Status
from dbfresh.store import Store
from dbfresh.tui.app import DbfreshApp
from dbfresh.tui.dashboard import (
    GridRow,
    header_key,
    hide_empty_off_days,
    populate_grid,
    trailing_dates,
)
from dbfresh.tui.screens import ObjectDetailScreen

# Tue 2026-07-14: the window runs Wed 07-08 .. Tue 07-14, with Sat 07-11
# and Sun 07-12 as its non-business days.
_TODAY = date(2026, 7, 14)
_SAT = date(2026, 7, 11)
_SUN = date(2026, 7, 12)


def _row(key, data_days):
    dates = trailing_dates(_TODAY)
    return GridRow(
        key=key,
        label=key,
        overall=Status.OK if data_days else None,
        days=[
            (Status.OK, None) if d in data_days else (None, None)
            for d in dates
        ],
    )


def _kept(rows, today=_TODAY, calendar=None):
    trimmed, dates = hide_empty_off_days(
        rows, trailing_dates(today), today, calendar
    )
    for row in trimmed:
        assert len(row.days) == len(dates)
    return dates, trimmed


# -- the selection rule ----------------------------------------------------


def test_empty_weekend_days_are_hidden():
    dates, _ = _kept([_row("a", set())])
    assert _SAT not in dates
    assert _SUN not in dates
    assert len(dates) == 5


def test_a_weekend_day_with_data_in_any_row_is_kept():
    dates, trimmed = _kept([_row("a", set()), _row("b", {_SUN})])
    assert _SUN in dates
    assert _SAT not in dates
    # Each row's cells still line up with the kept dates.
    sunday = dates.index(_SUN)
    assert trimmed[1].days[sunday] == (Status.OK, None)
    assert trimmed[0].days[sunday] == (None, None)


def test_today_is_kept_even_when_it_is_an_empty_weekend_day():
    dates, _ = _kept([_row("a", set())], today=_SAT)
    assert dates[-1] == _SAT


def test_an_empty_holiday_is_hidden_and_a_holiday_with_data_kept():
    calendar = build_calendar(
        {
            "timezone": "UTC",
            "holidays": {"extra": ["2026-07-09", "2026-07-10"]},
        }
    )
    dates, _ = _kept([_row("a", {date(2026, 7, 10)})], calendar=calendar)
    assert date(2026, 7, 9) not in dates
    assert date(2026, 7, 10) in dates


def test_a_calendar_that_works_saturdays_keeps_saturday():
    calendar = build_calendar(
        {
            "timezone": "UTC",
            "workdays": ["mon", "tue", "wed", "thu", "fri", "sat"],
        }
    )
    dates, _ = _kept([_row("a", set())], calendar=calendar)
    assert _SAT in dates
    assert _SUN not in dates


def test_without_a_calendar_business_days_are_monday_to_friday():
    dates, _ = _kept([])
    assert [d.strftime("%a") for d in dates] == [
        "Wed",
        "Thu",
        "Fri",
        "Mon",
        "Tue",
    ]


# -- populate_grid with a reduced set of dates ----------------------------


class _GridApp(App):
    def compose(self) -> ComposeResult:
        yield DataTable(id="grid")


def test_populate_grid_builds_columns_from_the_given_dates():
    async def scenario():
        rows, dates = hide_empty_off_days(
            [
                GridRow(
                    key="s\x1fa",
                    label="s.a",
                    overall=None,
                    days=[(None, None)] * 7,
                    source="s",
                    object="a",
                )
            ],
            trailing_dates(_TODAY),
            _TODAY,
            None,
        )
        app = _GridApp()
        async with app.run_test():
            table = app.query_one(DataTable)
            populate_grid(
                table,
                rows,
                _TODAY,
                label_header="object",
                group_headers=True,
                dates=dates,
            )
            keys = [str(k.value) for k in table.columns]
            assert keys == ["label", "overall"] + [
                d.isoformat() for d in dates
            ]
            headers = [str(c.label) for c in table.columns.values()]
            assert headers[2:] == ["Wed", "Thu", "Fri", "Mon", "Tue"]
            # The source header row is as wide as the trimmed columns.
            assert len(table.get_row(header_key("s"))) == len(keys)

    asyncio.run(scenario())


# -- the live grids --------------------------------------------------------

_CONFIG = """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: quiet
    metric: row_count
    expect: { min: 1 }
  - source: s
    object: busy
    metric: row_count
    expect: { min: 1 }
"""


def _frozen(now):
    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz is not None else now

    return _FrozenDateTime


def _setup(tmp_path, monkeypatch, now, seeded):
    """Freeze the TUI's clock at ``now`` and seed one observation per
    ``(object, status, observed_at)`` in ``seeded``."""
    frozen = _frozen(now)
    monkeypatch.setattr("dbfresh.tui.app.datetime", frozen)
    monkeypatch.setattr("dbfresh.tui.screens.datetime", frozen)
    monkeypatch.setattr("dbfresh.tui.app.display_timezone", lambda cal: UTC)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CONFIG)
    ids = {c.object: check_id(c) for c in load_config(cfg, env={}).checks}
    store_path = tmp_path / "obs.db"
    store = Store(store_path)
    for obj, status, observed_at in seeded:
        run_id = store.start_run()
        store.record_observation(
            run_id,
            Result(
                object=obj,
                metric="row_count",
                status=status,
                source="s",
                value=1,
                check_id=ids[obj],
            ),
            observed_at=observed_at,
        )
        store.finish_run(run_id, status)
    store.close()
    return DbfreshApp(config_path=cfg, store_path=str(store_path))


def _day_headers(table):
    return [str(c.label) for c in list(table.columns.values())[2:]]


_MONDAY_NOON = datetime(2026, 7, 13, 12, tzinfo=UTC)
_SUNDAY_NOON = datetime(2026, 7, 12, 12, tzinfo=UTC)


def test_home_grid_hides_an_empty_saturday_and_keeps_a_sunday_with_data(
    tmp_path, monkeypatch
):
    app = _setup(
        tmp_path,
        monkeypatch,
        _MONDAY_NOON,
        [
            ("quiet", Status.OK, _SUNDAY_NOON),
            ("busy", Status.FAIL, _MONDAY_NOON),
        ],
    )

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#dashboard-grid", DataTable)
            assert _day_headers(table) == [
                "Tue",
                "Wed",
                "Thu",
                "Fri",
                "Sun",
                "Mon",
            ]

            # The non-OK filter hides "quiet", the only row with Sunday
            # data; the columns are decided from every row, so Sunday stays.
            await pilot.press("f")
            await pilot.pause()
            assert "Sun" in _day_headers(table)

    asyncio.run(scenario())


def test_object_detail_grid_decides_from_its_own_rows(tmp_path, monkeypatch):
    app = _setup(
        tmp_path,
        monkeypatch,
        _MONDAY_NOON,
        [
            ("quiet", Status.OK, _SUNDAY_NOON),
            ("busy", Status.FAIL, _MONDAY_NOON),
        ],
    )

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            # Rows sort by object: "busy" first; it has no Sunday data.
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ObjectDetailScreen)
            table = app.screen.query_one(DataTable)
            assert "Sun" not in _day_headers(table)
            assert "Sat" not in _day_headers(table)

    asyncio.run(scenario())


def test_a_run_started_on_an_empty_saturday_does_not_repaint(
    tmp_path, monkeypatch
):
    # Today is kept even with no data, so the run-start check for a stale
    # window finds today's column and leaves the grid (and cursor) alone.
    saturday = datetime(2026, 7, 11, 9, tzinfo=UTC)
    app = _setup(tmp_path, monkeypatch, saturday, [])

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#dashboard-grid", DataTable)
            assert _day_headers(table)[-1] == "Sat"
            repaints = []
            monkeypatch.setattr(
                app, "refresh_dashboard", lambda: repaints.append(1)
            )
            app._repaint_stale_day_columns()
            assert repaints == []

    asyncio.run(scenario())
