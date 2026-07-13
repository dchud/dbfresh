import io
import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from rich.console import Console

from dbfresh.engine import Result, RunResult, Status
from dbfresh.report import (
    progress_reporter,
    render_digest,
    render_json,
    show_progress,
)


def test_digest_header_and_failure_block():
    run = RunResult(
        results=[
            Result(
                source="s", object="a", metric="row_count", status=Status.OK, value=5
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
    assert "✗ s.b · null_rate" in text
    assert "expected max 0.01   observed 0.2" in text
    assert "s.a" not in text  # OK checks are not listed


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
    run = RunResult(results=[], status=Status.OK, started_at=None, finished_at=None)
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


def test_show_progress_suppressed_by_json():
    assert show_progress(json_output=True, no_progress=False) is False


def test_show_progress_suppressed_by_no_progress_flag():
    assert show_progress(json_output=False, no_progress=True) is False


def test_show_progress_suppressed_when_stdout_is_not_a_tty():
    class _NotATty:
        def isatty(self):
            return False

    stream = _NotATty()
    assert show_progress(json_output=False, no_progress=False, stream=stream) is False


def test_show_progress_enabled_for_a_normal_interactive_run():
    class _Tty:
        def isatty(self):
            return True

    assert show_progress(json_output=False, no_progress=False, stream=_Tty()) is True


def test_progress_reporter_disabled_yields_a_noop_callback():
    with progress_reporter(total=3, enabled=False) as on_result:
        on_result(None)  # never raises regardless of what is passed


def test_progress_reporter_enabled_renders_completed_count():
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=True)
    with progress_reporter(total=2, enabled=True, console=console) as on_result:
        on_result(None)
        on_result(None)
    assert "2/2" in buffer.getvalue()
