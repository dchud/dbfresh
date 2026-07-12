from datetime import UTC, datetime

from dbfresh.engine import Result, RunResult, Status
from dbfresh.report import render_digest


def test_digest_shows_schema_drift_lines():
    run = RunResult(
        results=[
            Result(
                source="s",
                object="t",
                metric="schema",
                status=Status.FAIL,
                value="email:TEXT|id:BIGINT|name:TEXT",
                expected="unchanged",
                diff=["+ email (TEXT)", "~ id (INTEGER -> BIGINT)"],
            ),
        ],
        status=Status.FAIL,
    )
    text = render_digest(run, now=datetime(2026, 7, 10, tzinfo=UTC))
    assert "✗ s.t · schema" in text
    assert "+ email (TEXT)" in text
    assert "~ id (INTEGER -> BIGINT)" in text


def test_digest_schema_fail_without_diff_falls_back_to_expected_observed():
    run = RunResult(
        results=[
            Result(
                source="s",
                object="t",
                metric="schema",
                status=Status.FAIL,
                value="id:INTEGER",
                expected="equals id:BIGINT",
            ),
        ],
        status=Status.FAIL,
    )
    text = render_digest(run, now=datetime(2026, 7, 10, tzinfo=UTC))
    assert "expected equals id:BIGINT   observed id:INTEGER" in text


def test_digest_schema_error_shows_message_not_diff():
    run = RunResult(
        results=[
            Result(
                source="s",
                object="t",
                metric="schema",
                status=Status.ERROR,
                error="no such table: t",
                diff=["+ email (TEXT)"],  # should never happen, but error wins
            ),
        ],
        status=Status.ERROR,
    )
    text = render_digest(run, now=datetime(2026, 7, 10, tzinfo=UTC))
    assert "no such table: t" in text
    assert "+ email (TEXT)" not in text
