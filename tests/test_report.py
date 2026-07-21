import io
import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from rich.console import Console

from dbfresh.calendar import build_calendar
from dbfresh.engine import Result, RunResult, Status
from dbfresh.report import (
    display_timezone,
    progress_reporter,
    reconstruct_run,
    render_digest,
    render_json,
    show_progress,
)


def test_digest_header_and_failure_block():
    run = RunResult(
        results=[
            Result(
                source="s",
                object="a",
                metric="row_count",
                status=Status.OK,
                value=5,
            ),
            Result(
                source="s",
                object="b",
                metric="null_rate",
                status=Status.FAIL,
                value=0.2,
                expected="max 0.01",
            ),
        ],
        status=Status.FAIL,
    )
    now = datetime(2026, 7, 10, 6, 3, tzinfo=UTC)
    text = render_digest(run, now=now)

    assert "DATA CHECK REPORT — 2026-07-10T06:03:00Z" in text
    assert "2 checks · 1 passed · 1 failed" in text
    assert "✗ s.b · null_rate — expected max 0.01" in text
    assert "observed: 20.0%" in text
    assert "s.a" not in text  # OK checks are not listed


def test_digest_duplicate_count_observed_as_plain_integer():
    run = RunResult(
        results=[
            Result(
                source="s",
                object="orders",
                metric="duplicate_count",
                status=Status.FAIL,
                value=1.0,
                expected="max 0",
            ),
        ],
        status=Status.FAIL,
    )
    text = render_digest(run, now=datetime(2026, 7, 10, tzinfo=UTC))
    assert "observed: 1" in text
    assert "observed: 1.0" not in text


def test_digest_row_count_observed_as_plain_integer():
    run = RunResult(
        results=[
            Result(
                source="s",
                object="orders",
                metric="row_count",
                status=Status.FAIL,
                value=60.0,
                expected="between 1 and 10",
            ),
        ],
        status=Status.FAIL,
    )
    text = render_digest(run, now=datetime(2026, 7, 10, tzinfo=UTC))
    assert "observed: 60" in text
    assert "observed: 60.0" not in text


def test_digest_avg_observed_rounded_to_two_decimals():
    run = RunResult(
        results=[
            Result(
                source="s",
                object="orders",
                metric="avg",
                status=Status.FAIL,
                value=123.456789,
                expected="vs_previous(previous) ratio [0.5, 2.0]",
            ),
        ],
        status=Status.FAIL,
    )
    text = render_digest(run, now=datetime(2026, 7, 10, tzinfo=UTC))
    assert "observed: 123.46" in text


def test_digest_freshness_observed_as_duration_with_reconstructed_timestamp():
    now = datetime(2026, 7, 14, 12, 44, 27, tzinfo=UTC)
    run = RunResult(
        results=[
            Result(
                source="s",
                object="orders",
                metric="freshness",
                status=Status.FAIL,
                value=464533.484447,  # ~5d 9h
                expected="max_lag 24h",
            ),
        ],
        status=Status.FAIL,
        started_at=now,
    )
    text = render_digest(run, now=now)
    assert "✗ s.orders · freshness — expected max_lag 24h" in text
    assert "observed: 5d 9h stale (last update: 2026-07-09T03:42:13Z)" in text


def test_digest_freshness_without_run_started_at_still_shows_duration():
    now = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)
    run = RunResult(
        results=[
            Result(
                source="s",
                object="orders",
                metric="freshness",
                status=Status.FAIL,
                value=90.0,
                expected="max_lag 24h",
            ),
        ],
        status=Status.FAIL,
        started_at=None,
    )
    text = render_digest(run, now=now)
    assert "observed: 1m 30s stale (last update: 2026-07-14T11:58:30Z)" in text


def test_display_timezone_prefers_configured_calendar_zone():
    calendar = build_calendar({"timezone": "America/New_York"})
    assert display_timezone(calendar) == ZoneInfo("America/New_York")


def test_display_timezone_defaults_to_local_system_time_without_calendar():
    local_offset = datetime.now().astimezone().tzinfo.utcoffset(None)
    result = display_timezone(None)
    assert result is not None
    assert result.utcoffset(None) == local_offset


def test_digest_header_uses_configured_display_timezone():
    run = RunResult(results=[], status=Status.OK)
    now = datetime(2026, 7, 10, 6, 3, tzinfo=UTC)
    text = render_digest(run, now=now, tz=ZoneInfo("America/New_York"))

    assert "DATA CHECK REPORT — 2026-07-10T02:03:00-04:00" in text


def test_digest_error_shows_message():
    run = RunResult(
        results=[
            Result(
                source="s",
                object="down",
                metric="row_count",
                status=Status.ERROR,
                error="no such table: down",
            ),
        ],
        status=Status.ERROR,
    )
    text = render_digest(run, now=datetime(2026, 7, 10, tzinfo=UTC))
    assert "no such table: down" in text


def _envelope_run():
    return RunResult(
        results=[
            Result(
                source="s",
                object="a",
                metric="row_count",
                status=Status.OK,
                value=5,
                expected="between 1 and 10",
                tier="table",
            ),
            Result(
                source="s",
                object="b",
                metric="null_rate",
                status=Status.FAIL,
                value=0.2,
                expected="max 0.01",
                tier="column",
            ),
        ],
        status=Status.FAIL,
        run_id=7,
        started_at=datetime(2026, 7, 10, 6, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 7, 10, 6, 0, 5, tzinfo=UTC),
    )


def test_render_json_envelope_has_run_metadata_and_counts():
    payload = json.loads(render_json(_envelope_run()))

    assert payload["status"] == "FAIL"
    assert payload["run_id"] == 7
    assert payload["started_at"] == "2026-07-10T06:00:00Z"
    assert payload["finished_at"] == "2026-07-10T06:00:05Z"
    assert payload["counts"] == {
        "OK": 1,
        "WARN": 0,
        "FAIL": 1,
        "ERROR": 0,
        "SKIPPED": 0,
    }


def test_render_json_envelope_run_id_null_without_store():
    run = RunResult(
        results=[], status=Status.OK, started_at=None, finished_at=None
    )
    payload = json.loads(render_json(run))

    assert payload["run_id"] is None
    assert payload["started_at"] is None
    assert payload["finished_at"] is None


def test_render_json_result_has_tier_value_text_observed_and_diff():
    payload = json.loads(render_json(_envelope_run()))
    results = {r["object"]: r for r in payload["results"]}

    table_result = results["a"]
    assert table_result["tier"] == "table"
    assert table_result["value"] == 5.0
    assert table_result["value_text"] is None
    assert table_result["observed"] == "5"
    assert table_result["diff"] is None

    column_result = results["b"]
    assert column_result["tier"] == "column"
    assert column_result["value"] == 0.2
    assert column_result["value_text"] is None
    assert column_result["observed"] == "0.2"


def test_render_json_schema_result_puts_fingerprint_in_value_text():
    run = RunResult(
        results=[
            Result(
                source="s",
                object="t",
                metric="schema",
                status=Status.FAIL,
                value="email:TEXT|id:BIGINT",
                expected="unchanged",
                diff=["+ email (TEXT)"],
                tier="table",
            ),
        ],
        status=Status.FAIL,
    )
    payload = json.loads(render_json(run))
    result = payload["results"][0]

    assert result["value"] is None
    assert result["value_text"] == "email:TEXT|id:BIGINT"
    assert result["observed"] == "email:TEXT|id:BIGINT"
    assert result["diff"] == ["+ email (TEXT)"]


def test_render_json_assertion_result_has_label_and_null_value_text():
    run = RunResult(
        results=[
            Result(
                source="s",
                object="t",
                metric=None,
                status=Status.FAIL,
                value=3,
                label="assert amount >= 0",
                samples=[{"amount": -1.0}],
                tier="table",
            ),
        ],
        status=Status.FAIL,
    )
    payload = json.loads(render_json(run))
    result = payload["results"][0]

    assert result["label"] == "assert amount >= 0"
    assert result["value"] == 3.0
    assert result["value_text"] is None
    assert result["observed"] == "3"
    assert result["samples"] == [{"amount": -1.0}]


def _observation(**overrides) -> dict:
    fields = {
        "run_id": 1,
        "check_id": "abc",
        "source": "s",
        "object": "orders",
        "metric": "row_count",
        "label": "row_count",
        "value": 5.0,
        "value_text": None,
        "status": "OK",
        "observed_at": "2026-07-15T14:00:00+00:00",
        "weekday": 2,
        "expected": None,
        "error": None,
    }
    fields.update(overrides)
    return fields


def _run_row(**overrides) -> dict:
    fields = {
        "run_id": 1,
        "started_at": "2026-07-15T13:59:55+00:00",
        "finished_at": "2026-07-15T14:00:05+00:00",
        "status": "FAIL",
        "git_sha": None,
    }
    fields.update(overrides)
    return fields


def test_reconstruct_run_builds_a_result_per_observation():
    run = reconstruct_run(
        _run_row(),
        [
            _observation(check_id="a", value=5.0),
            _observation(
                check_id="b",
                metric="null_rate",
                label="null_rate",
                status="FAIL",
                value=0.2,
                expected="max 0.05",
            ),
        ],
    )
    assert run.status == Status.FAIL
    assert run.run_id == 1
    assert run.started_at == datetime(2026, 7, 15, 13, 59, 55, tzinfo=UTC)
    assert run.finished_at == datetime(2026, 7, 15, 14, 0, 5, tzinfo=UTC)
    assert [r.status for r in run.results] == [Status.OK, Status.FAIL]
    assert run.results[1].expected == "max 0.05"


def test_reconstruct_run_falls_back_to_value_text_for_non_numeric_observations():
    run = reconstruct_run(
        _run_row(),
        [_observation(metric="schema", value=None, value_text="email:TEXT")],
    )
    assert run.results[0].value == "email:TEXT"


def test_reconstruct_run_carries_error_text():
    run = reconstruct_run(
        _run_row(),
        [
            _observation(
                status="ERROR",
                value=None,
                error="connection refused",
                expected=None,
            )
        ],
    )
    assert run.results[0].error == "connection refused"


def test_reconstruct_run_never_carries_samples_or_diff():
    """The store never persists violating-row samples or a schema diff --
    every reconstructed Result must leave both unset so render_digest falls
    back to its plain "observed: <value>" line rather than crashing on
    data that was never there."""
    run = reconstruct_run(_run_row(), [_observation()])
    assert run.results[0].samples is None
    assert run.results[0].diff is None


def test_reconstruct_run_digest_renders_through_render_digest():
    run = reconstruct_run(
        _run_row(),
        [
            _observation(check_id="a", status="OK", value=5.0),
            _observation(
                check_id="b",
                metric="null_rate",
                label="null_rate",
                status="FAIL",
                value=0.2,
                expected="max 0.05",
            ),
        ],
    )
    text = render_digest(run, now=datetime(2026, 7, 15, 14, 0, 5, tzinfo=UTC))
    assert "✗ s.orders · null_rate — expected max 0.05" in text
    assert "observed: 20.0%" in text


def test_show_progress_suppressed_by_json():
    assert show_progress(json_output=True, no_progress=False) is False


def test_show_progress_suppressed_by_no_progress_flag():
    assert show_progress(json_output=False, no_progress=True) is False


def test_show_progress_suppressed_when_stdout_is_not_a_tty():
    class _NotATty:
        def isatty(self):
            return False

    stream = _NotATty()
    assert (
        show_progress(json_output=False, no_progress=False, stream=stream)
        is False
    )


def test_show_progress_enabled_for_a_normal_interactive_run():
    class _Tty:
        def isatty(self):
            return True

    assert (
        show_progress(json_output=False, no_progress=False, stream=_Tty())
        is True
    )


def test_progress_reporter_disabled_yields_a_noop_callback():
    with progress_reporter(total=3, enabled=False) as on_result:
        on_result(None)  # never raises regardless of what is passed


def test_progress_reporter_enabled_renders_completed_count():
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=True)
    with progress_reporter(
        total=2, enabled=True, console=console
    ) as on_result:
        on_result(None)
        on_result(None)
    assert "2/2" in buffer.getvalue()
