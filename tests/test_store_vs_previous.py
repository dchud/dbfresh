from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from dbfresh.calendar import build_calendar
from dbfresh.engine import Result, Status
from dbfresh.store import Store


def _result(**overrides) -> Result:
    fields = {
        "object": "dbo.fct_sales",
        "metric": "row_count",
        "status": Status.OK,
        "source": "warehouse",
        "value": 42,
        "check_id": "abc123def456",
    }
    fields.update(overrides)
    return Result(**fields)


def test_latest_clean_observation_skips_error_and_skipped(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    store.record_observation(
        run_id,
        _result(check_id="x", value=1, status=Status.OK),
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    store.record_observation(
        run_id,
        _result(check_id="x", value=999, status=Status.ERROR),
        observed_at=datetime(2026, 7, 2, tzinfo=UTC),
    )
    store.record_observation(
        run_id,
        _result(check_id="x", value=888, status=Status.SKIPPED),
        observed_at=datetime(2026, 7, 3, tzinfo=UTC),
    )
    obs = store.latest_clean_observation("x")
    assert obs["value"] == 1.0
    store.close()


def test_latest_clean_observation_returns_none_when_only_dirty_statuses(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    store.record_observation(run_id, _result(check_id="x", status=Status.ERROR))
    store.record_observation(run_id, _result(check_id="x", status=Status.SKIPPED))
    assert store.latest_clean_observation("x") is None
    store.close()


def test_latest_clean_observation_returns_none_with_no_history(tmp_path):
    store = Store(tmp_path / "obs.db")
    assert store.latest_clean_observation("nonexistent") is None
    store.close()


def test_latest_clean_observation_picks_most_recent_clean(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    store.record_observation(
        run_id,
        _result(check_id="x", value=10, status=Status.OK),
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    store.record_observation(
        run_id,
        _result(check_id="x", value=20, status=Status.WARN),
        observed_at=datetime(2026, 7, 2, tzinfo=UTC),
    )
    obs = store.latest_clean_observation("x")
    assert obs["value"] == 20.0
    store.close()


def test_last_same_weekday_observation_matches_two_weeks_back(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    base = datetime(2026, 7, 6, tzinfo=UTC)
    store.record_observation(
        run_id, _result(check_id="x", value=100, status=Status.OK), observed_at=base
    )
    run_date = (base + timedelta(days=14)).date()  # same weekday, 2 weeks later
    obs = store.last_same_weekday_observation("x", run_date)
    assert obs["value"] == 100.0
    store.close()


def test_last_same_weekday_observation_excludes_same_day_rerun(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    base = datetime(2026, 7, 6, tzinfo=UTC)
    store.record_observation(
        run_id, _result(check_id="x", value=100, status=Status.OK), observed_at=base
    )
    run_date = base.date()  # today, same day -> within the 6-day floor
    assert store.last_same_weekday_observation("x", run_date) is None
    store.close()


def test_last_same_weekday_observation_excludes_wrong_weekday(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    base = datetime(2026, 7, 6, tzinfo=UTC)
    store.record_observation(
        run_id, _result(check_id="x", value=100, status=Status.OK), observed_at=base
    )
    run_date = (base + timedelta(days=15)).date()  # one day off, different weekday
    assert store.last_same_weekday_observation("x", run_date) is None
    store.close()


def test_last_same_weekday_observation_excludes_error_and_skipped(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    base = datetime(2026, 7, 6, tzinfo=UTC)
    store.record_observation(
        run_id,
        _result(check_id="x", value=999, status=Status.ERROR),
        observed_at=base,
    )
    run_date = (base + timedelta(days=14)).date()
    assert store.last_same_weekday_observation("x", run_date) is None
    store.close()


def test_last_same_weekday_observation_picks_most_recent_matching(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    base = datetime(2026, 7, 6, tzinfo=UTC)
    store.record_observation(
        run_id,
        _result(check_id="x", value=100, status=Status.OK),
        observed_at=base,
    )
    store.record_observation(
        run_id,
        _result(check_id="x", value=200, status=Status.OK),
        observed_at=base + timedelta(days=14),
    )
    run_date = (base + timedelta(days=28)).date()
    obs = store.last_same_weekday_observation("x", run_date)
    assert obs["value"] == 200.0
    store.close()


def test_last_same_weekday_observation_floor_boundary_at_exactly_6_days(tmp_path):
    # Directly control weekday/observed_at to isolate the floor arithmetic
    # from the natural 7-day weekday cycle: a prior observation dated
    # exactly 6 calendar days before run_date, sharing run_date's weekday
    # value, must match; one day later (5 days before) must not.
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    run_date = date(2026, 7, 19)
    matching_weekday = run_date.weekday()
    exactly_6_days_back = run_date - timedelta(days=6)
    store._conn.execute(
        "INSERT INTO observation (run_id, check_id, source, object, metric, "
        "label, value, value_text, status, observed_at, weekday) "
        "VALUES (?, 'x', 's', 'o', 'row_count', 'row_count', 42, NULL, 'OK', "
        "?, ?)",
        (run_id, exactly_6_days_back.isoformat(), matching_weekday),
    )
    store._conn.commit()
    obs = store.last_same_weekday_observation("x", run_date)
    assert obs["value"] == 42.0
    store.close()


def test_last_same_weekday_observation_floor_boundary_at_5_days_excluded(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    run_date = date(2026, 7, 19)
    matching_weekday = run_date.weekday()
    exactly_5_days_back = run_date - timedelta(days=5)
    store._conn.execute(
        "INSERT INTO observation (run_id, check_id, source, object, metric, "
        "label, value, value_text, status, observed_at, weekday) "
        "VALUES (?, 'x', 's', 'o', 'row_count', 'row_count', 42, NULL, 'OK', "
        "?, ?)",
        (run_id, exactly_5_days_back.isoformat(), matching_weekday),
    )
    store._conn.commit()
    assert store.last_same_weekday_observation("x", run_date) is None
    store.close()


def test_record_observation_stores_weekday_in_calendar_timezone(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    cal = build_calendar({"timezone": "America/Los_Angeles"})
    observed_at = datetime(2026, 7, 6, 1, 0, tzinfo=UTC)
    local_weekday = observed_at.astimezone(ZoneInfo("America/Los_Angeles")).weekday()
    assert local_weekday != observed_at.weekday()  # sanity: the tz crosses midnight

    store.record_observation(
        run_id, _result(check_id="x"), observed_at=observed_at, calendar=cal
    )
    row = store._conn.execute("SELECT weekday FROM observation").fetchone()
    assert row["weekday"] == local_weekday
    store.close()


def test_record_observation_without_calendar_stores_utc_weekday(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    observed_at = datetime(2026, 7, 6, 1, 0, tzinfo=UTC)
    store.record_observation(run_id, _result(check_id="x"), observed_at=observed_at)
    row = store._conn.execute("SELECT weekday FROM observation").fetchone()
    assert row["weekday"] == observed_at.weekday()
    store.close()
