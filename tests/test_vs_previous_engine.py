from datetime import UTC, datetime, timedelta

from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.calendar import build_calendar
from dbfresh.checks import Check, check_id, parse_expectation
from dbfresh.engine import Result, Status, evaluate_check, run_checks
from dbfresh.store import Store


def _rows_adapter(n):
    a = SqliteAdapter()
    a.rows("CREATE TABLE t (id INTEGER)")
    for i in range(n):
        a.rows(f"INSERT INTO t (id) VALUES ({i})")
    return a


def _vs_previous_check(**vs_previous_overrides):
    spec = {"baseline": "previous", "min_ratio": 0.5, "max_ratio": 2.0}
    spec.update(vs_previous_overrides)
    return Check(
        source="s",
        object="t",
        metric="row_count",
        expect=parse_expectation({"vs_previous": spec}, metric="row_count"),
    )


def _stored_baseline(store, cid, value, status=Status.OK, observed_at=None):
    run_id = store.start_run()
    result = Result(
        object="t",
        metric="row_count",
        status=status,
        source="s",
        value=value,
        check_id=cid,
    )
    store.record_observation(run_id, result, observed_at=observed_at)


def test_no_store_defaults_to_on_missing_pass():
    a = _rows_adapter(5)
    check = _vs_previous_check()
    result = evaluate_check(check, a)
    assert result.status == Status.OK
    assert result.value == 5
    a.close()


def test_no_store_on_missing_warn():
    a = _rows_adapter(5)
    check = _vs_previous_check(on_missing="warn")
    result = evaluate_check(check, a)
    assert result.status == Status.WARN
    a.close()


def test_no_store_on_missing_skip():
    a = _rows_adapter(5)
    check = _vs_previous_check(on_missing="skip")
    result = evaluate_check(check, a)
    assert result.status == Status.SKIPPED
    a.close()


def test_first_run_with_store_but_no_history_uses_on_missing(tmp_path):
    a = _rows_adapter(5)
    store = Store(tmp_path / "obs.db")
    check = _vs_previous_check()
    result = evaluate_check(check, a, store=store)
    assert result.status == Status.OK
    a.close()
    store.close()


def test_baseline_previous_within_ratio_passes(tmp_path):
    a = _rows_adapter(100)
    store = Store(tmp_path / "obs.db")
    check = _vs_previous_check()
    _stored_baseline(store, check_id(check), value=100)
    result = evaluate_check(check, a, store=store)
    assert result.status == Status.OK
    assert result.value == 100
    a.close()
    store.close()


def test_baseline_previous_3x_swing_fails(tmp_path):
    a = _rows_adapter(350)
    store = Store(tmp_path / "obs.db")
    check = _vs_previous_check()
    _stored_baseline(store, check_id(check), value=100)
    result = evaluate_check(check, a, store=store)
    assert result.status == Status.FAIL
    a.close()
    store.close()


def test_baseline_previous_violation_with_warn_severity_yields_warn(tmp_path):
    a = _rows_adapter(350)
    store = Store(tmp_path / "obs.db")
    check = _vs_previous_check()
    check.severity = "warn"
    _stored_baseline(store, check_id(check), value=100)
    result = evaluate_check(check, a, store=store)
    assert result.status == Status.WARN
    a.close()
    store.close()


def test_baseline_previous_excludes_error_observations(tmp_path):
    a = _rows_adapter(350)  # would fail against a value=100 baseline
    store = Store(tmp_path / "obs.db")
    check = _vs_previous_check()
    _stored_baseline(store, check_id(check), value=100, status=Status.ERROR)
    result = evaluate_check(check, a, store=store)
    assert result.status == Status.OK  # no clean baseline -> on_missing pass
    a.close()
    store.close()


def test_baseline_previous_excludes_skipped_observations(tmp_path):
    a = _rows_adapter(350)
    store = Store(tmp_path / "obs.db")
    check = _vs_previous_check()
    _stored_baseline(store, check_id(check), value=100, status=Status.SKIPPED)
    result = evaluate_check(check, a, store=store)
    assert result.status == Status.OK  # no clean baseline -> on_missing pass
    a.close()
    store.close()


def test_zero_baseline_without_delta_guard_falls_back_to_on_missing(tmp_path):
    a = _rows_adapter(5)
    store = Store(tmp_path / "obs.db")
    check = _vs_previous_check(on_missing="warn")
    _stored_baseline(store, check_id(check), value=0)
    result = evaluate_check(check, a, store=store)
    assert result.status == Status.WARN  # on_missing, not a ratio evaluation
    a.close()
    store.close()


def test_zero_baseline_with_delta_guard_uses_delta_instead(tmp_path):
    a = _rows_adapter(5)
    store = Store(tmp_path / "obs.db")
    check = _vs_previous_check(min_delta=-10, max_delta=10)
    _stored_baseline(store, check_id(check), value=0)
    result = evaluate_check(check, a, store=store)
    assert result.status == Status.OK  # 5 - 0 = 5, within [-10, 10]
    a.close()
    store.close()


def test_zero_baseline_with_delta_guard_can_still_fail(tmp_path):
    a = _rows_adapter(50)
    store = Store(tmp_path / "obs.db")
    check = _vs_previous_check(min_delta=-10, max_delta=10)
    _stored_baseline(store, check_id(check), value=0)
    result = evaluate_check(check, a, store=store)
    assert result.status == Status.FAIL  # 50 - 0 = 50, outside [-10, 10]
    a.close()
    store.close()


def test_delta_only_guard_evaluated_without_ratio(tmp_path):
    a = _rows_adapter(108)
    store = Store(tmp_path / "obs.db")
    check = _vs_previous_check(
        min_ratio=None, max_ratio=None, min_delta=-5, max_delta=5
    )
    _stored_baseline(store, check_id(check), value=100)
    result = evaluate_check(check, a, store=store)
    assert result.status == Status.FAIL  # 108 - 100 = 8, outside [-5, 5]
    a.close()
    store.close()


def test_ratio_and_delta_both_configured_both_must_pass(tmp_path):
    a = _rows_adapter(110)  # ratio 1.1 passes [0.5, 2.0]; delta 10 fails [-5, 5]
    store = Store(tmp_path / "obs.db")
    check = _vs_previous_check(min_delta=-5, max_delta=5)
    _stored_baseline(store, check_id(check), value=100)
    result = evaluate_check(check, a, store=store)
    assert result.status == Status.FAIL
    a.close()
    store.close()


def test_null_current_scalar_uses_empty_result_not_on_missing(tmp_path):
    a = SqliteAdapter()
    a.rows("CREATE TABLE t (amount REAL)")
    store = Store(tmp_path / "obs.db")
    check = Check(
        source="s",
        object="t",
        metric="avg",
        column="amount",
        expect=parse_expectation(
            {"vs_previous": {"baseline": "previous", "min_ratio": 0.5, "max_ratio": 2}},
            metric="avg",
        ),
    )
    result = evaluate_check(check, a, store=store)
    assert result.status == Status.FAIL  # empty result, allow_empty defaults False
    a.close()
    store.close()


def test_query_error_yields_error_status(tmp_path):
    a = SqliteAdapter()  # table "t" never created
    store = Store(tmp_path / "obs.db")
    check = _vs_previous_check()
    result = evaluate_check(check, a, store=store)
    assert result.status == Status.ERROR
    assert result.error is not None
    a.close()
    store.close()


def test_result_carries_check_id_and_expected_description(tmp_path):
    a = _rows_adapter(100)
    store = Store(tmp_path / "obs.db")
    check = _vs_previous_check()
    _stored_baseline(store, check_id(check), value=100)
    result = evaluate_check(check, a, store=store)
    assert result.check_id == check_id(check)
    assert "vs_previous" in result.expected
    a.close()
    store.close()


def test_run_checks_threads_store_through_for_vs_previous(tmp_path):
    a = _rows_adapter(350)
    store = Store(tmp_path / "obs.db")
    check = _vs_previous_check()
    _stored_baseline(store, check_id(check), value=100)
    run = run_checks({"s": a}, [check], store=store)
    assert run.results[0].status == Status.FAIL
    a.close()
    store.close()


def test_baseline_last_same_weekday_uses_calendar_timezone_and_floor(tmp_path):
    cal = build_calendar({"timezone": "UTC"})
    store = Store(tmp_path / "obs.db")
    check = _vs_previous_check(baseline="last_same_weekday")
    cid = check_id(check)

    two_weeks_ago = datetime(2026, 6, 22, tzinfo=UTC)  # same weekday, 2 weeks back
    run_id = store.start_run()
    result = Result(
        object="t",
        metric="row_count",
        status=Status.OK,
        source="s",
        value=100,
        check_id=cid,
    )
    store.record_observation(run_id, result, observed_at=two_weeks_ago, calendar=cal)

    a = _rows_adapter(350)
    now = two_weeks_ago + timedelta(days=14)
    outcome = evaluate_check(check, a, now=now, calendar=cal, store=store)
    assert outcome.status == Status.FAIL  # 350 vs 100 baseline, ratio 3.5
    a.close()
    store.close()


def test_baseline_last_same_weekday_no_match_within_floor_is_on_missing(tmp_path):
    cal = build_calendar({"timezone": "UTC"})
    store = Store(tmp_path / "obs.db")
    check = _vs_previous_check(baseline="last_same_weekday")
    cid = check_id(check)

    now = datetime(2026, 7, 6, tzinfo=UTC)
    run_id = store.start_run()
    result = Result(
        object="t",
        metric="row_count",
        status=Status.OK,
        source="s",
        value=999,
        check_id=cid,
    )
    store.record_observation(run_id, result, observed_at=now, calendar=cal)  # same day

    a = _rows_adapter(5)
    outcome = evaluate_check(check, a, now=now, calendar=cal, store=store)
    assert outcome.status == Status.OK  # no eligible baseline -> on_missing pass
    a.close()
    store.close()
