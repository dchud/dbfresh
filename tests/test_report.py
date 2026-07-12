from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from dbfresh.engine import Result, RunResult, Status
from dbfresh.report import render_digest


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
