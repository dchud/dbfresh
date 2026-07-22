"""Unit tests for presentation helpers in dbfresh.tui.screens.

Full-screen rendering (including these helpers' output) is covered by the
snapshot suite in test_tui_snapshots.py; these tests check the helpers'
text/style output directly, without needing a running app.
"""

from __future__ import annotations

from dbfresh.models import Result, RunResult, Status
from dbfresh.report import render_digest, render_history
from dbfresh.tui.dashboard import status_glyph, status_style
from dbfresh.tui.screens import (
    _colorized_digest,
    _colorized_history,
    _digest_segments,
)

_CANDIDATE = {
    "check_id": "aaa111222333444555",
    "source": "warehouse",
    "object": "dbo.fct_sales",
    "metric": "row_count",
    "label": "row_count",
}


def _row(
    observed_at: str,
    status: str,
    value: float | None,
    expected: str | None = None,
    error: str | None = None,
) -> dict:
    return {
        "observed_at": observed_at,
        "status": status,
        "value": value,
        "value_text": None,
        "expected": expected,
        "error": error,
    }


def test_colorized_history_drops_check_id_hash_from_heading():
    text = _colorized_history(_CANDIDATE, [], tz=None)
    assert _CANDIDATE["check_id"] not in text.plain
    assert "warehouse.dbo.fct_sales" in text.plain


def test_render_history_still_includes_check_id_the_tui_view_drops():
    """The hash removal is TUI-only -- the CLI's `dbfresh history` output
    (render_history itself) is untouched."""
    plain = render_history(_CANDIDATE, [], tz=None)
    assert _CANDIDATE["check_id"] in plain


def test_colorized_history_handles_no_observations():
    text = _colorized_history(_CANDIDATE, [], tz=None)
    assert "no observations" in text.plain.lower()


def test_colorized_history_recolors_each_row_status():
    rows = [
        _row("2026-07-08T00:00:00+00:00", "OK", 10000.0),
        _row("2026-07-09T00:00:00+00:00", "WARN", 12000.0),
        _row("2026-07-10T00:00:00+00:00", "FAIL", 500.0),
        _row("2026-07-11T00:00:00+00:00", "ERROR", None),
        _row("2026-07-12T00:00:00+00:00", "SKIPPED", None),
    ]
    text = _colorized_history(_CANDIDATE, rows, tz=None)

    for row in rows:
        status = Status(row["status"])
        glyph = status_glyph(status)
        style = status_style(status)
        segments = [
            text.plain[span.start : span.end]
            for span in text.spans
            if span.style == style
        ]
        assert any(
            segment.strip().startswith(glyph) and row["status"] in segment
            for segment in segments
        )


def test_colorized_history_fail_and_error_stay_visually_distinct():
    rows = [
        _row("2026-07-08T00:00:00+00:00", "FAIL", 500.0),
        _row("2026-07-09T00:00:00+00:00", "ERROR", None),
    ]
    text = _colorized_history(_CANDIDATE, rows, tz=None)
    fail_style = next(
        span.style
        for span in text.spans
        if "FAIL" in text.plain[span.start : span.end]
    )
    error_style = next(
        span.style
        for span in text.spans
        if "ERROR" in text.plain[span.start : span.end]
    )
    assert fail_style != error_style


def test_colorized_history_shows_expected_and_error():
    rows = [
        _row(
            "2026-07-08T00:00:00+00:00",
            "FAIL",
            500.0,
            expected="between 1 and 100000",
        ),
        _row(
            "2026-07-09T00:00:00+00:00",
            "ERROR",
            None,
            error="connection refused",
        ),
    ]
    text = _colorized_history(_CANDIDATE, rows, tz=None).plain
    assert "between 1 and 100000" in text
    assert "connection refused" in text


def test_colorized_history_aligns_the_value_column_including_skipped():
    """The value column starts at the same position on every row. The
    "glyph SKIPPED" cell is one char wider than the others, so without the
    widened status field the SKIPPED row's value would drift right of the
    rest."""
    rows = [
        _row("2026-07-08T00:00:00+00:00", "OK", 10000.0),
        _row("2026-07-09T00:00:00+00:00", "SKIPPED", 10000.0),
    ]
    ok_line, skipped_line = _colorized_history(
        _CANDIDATE, rows, tz=None
    ).plain.split("\n")[-2:]
    assert "OK" in ok_line and "SKIPPED" in skipped_line
    assert ok_line.index("10000") == skipped_line.index("10000")


def test_colorized_history_has_no_trend_column():
    rows = [
        _row("2026-07-08T00:00:00+00:00", "OK", 10000.0),
        _row("2026-07-09T00:00:00+00:00", "OK", 12000.0),
        _row("2026-07-10T00:00:00+00:00", "FAIL", 500.0),
    ]
    text = _colorized_history(_CANDIDATE, rows, tz=None).plain
    assert "▲" not in text
    assert "▼" not in text
    assert "trend" not in text


def test_colorized_history_freshness_row_stays_aligned():
    """A freshness row's value ("<duration> stale (last update: <ts>)") is
    far wider than a plain number, but the status field -- which
    ``_colorized_history`` locates by fixed offset -- sits ahead of it, so
    recoloring still lands on the right slice of each line."""
    candidate = {**_CANDIDATE, "metric": "freshness", "label": "freshness"}
    rows = [_row("2026-07-14T12:44:27+00:00", "FAIL", 464533.484447)]
    text = _colorized_history(candidate, rows, tz=None)
    assert "5d 9h stale (last update: 2026-07-09  3:42 AM (Thu))" in text.plain

    style = status_style(Status.FAIL)
    glyph = status_glyph(Status.FAIL)
    segments = [
        text.plain[span.start : span.end]
        for span in text.spans
        if span.style == style
    ]
    assert any(
        segment.strip().startswith(glyph) and "FAIL" in segment
        for segment in segments
    )


# --- _digest_segments / _colorized_digest ----------------------------------


def _mixed_status_run() -> RunResult:
    """One OK, one FAIL, one WARN -- enough to check that the OK check is
    skipped and the two non-OK blocks come back in ``run.results`` order,
    each paired with its own ``check_id``."""
    return RunResult(
        results=[
            Result(
                source="s",
                object="a",
                metric="row_count",
                status=Status.OK,
                value=5,
                check_id="ok-check",
            ),
            Result(
                source="s",
                object="b",
                metric="null_rate",
                status=Status.FAIL,
                value=0.2,
                expected="max 0.01",
                check_id="fail-check",
            ),
            Result(
                source="s",
                object="c",
                metric="row_count",
                status=Status.WARN,
                value=0,
                expected="between 1 and 10",
                check_id="warn-check",
            ),
        ],
        status=Status.FAIL,
    )


def test_digest_segments_splits_non_ok_blocks_with_check_ids():
    run = _mixed_status_run()
    header, segments = _digest_segments(run, tz=None)

    assert "3 checks" in header.plain
    assert "1 passed" in header.plain
    assert "1 failed" in header.plain
    assert "1 warned" in header.plain

    assert len(segments) == 2
    (fail_result, fail_block), (warn_result, warn_block) = segments
    assert fail_result.check_id == "fail-check"
    assert warn_result.check_id == "warn-check"
    assert "s.b · null_rate" in fail_block.plain
    assert "s.c · row_count" in warn_block.plain


def test_digest_segments_all_ok_run_has_no_segments():
    run = RunResult(
        results=[
            Result(
                source="s",
                object="a",
                metric="row_count",
                status=Status.OK,
                value=5,
                check_id="ok-check",
            ),
        ],
        status=Status.OK,
    )
    header, segments = _digest_segments(run, tz=None)

    assert segments == []
    assert "DATA CHECK REPORT" in header.plain


def test_colorized_digest_preserves_render_digest_lines_verbatim():
    """``_colorized_digest`` is now built on top of ``_digest_segments`` --
    guard that the rebuild still reproduces ``render_digest``'s own plain
    text unchanged, line for line, except each block header's leading
    literal ``✗`` -- which was already swapped for the status's own glyph
    (``status_glyph``, e.g. WARN's ``!``) before this refactor too, so
    that substitution is not new behavior here, just preserved.

    The very first line (the "DATA CHECK REPORT — <timestamp>" header)
    is excluded: both functions resolve their own "now" independently, so
    comparing it verbatim would be a rare source of flakiness for no
    reason -- everything after it is deterministic from ``run`` alone.
    """
    run = _mixed_status_run()
    plain_lines = render_digest(run, tz=None).split("\n")
    colorized_lines = _colorized_digest(run, tz=None).plain.split("\n")
    assert len(plain_lines) == len(colorized_lines)
    for plain_line, colorized_line in zip(
        plain_lines[1:], colorized_lines[1:], strict=True
    ):
        if plain_line.startswith("✗ "):
            assert colorized_line[1:] == plain_line[1:]
        else:
            assert colorized_line == plain_line


def test_colorized_digest_recolors_each_blocks_header_line():
    run = _mixed_status_run()
    text = _colorized_digest(run, tz=None)

    fail_style = status_style(Status.FAIL)
    fail_glyph = status_glyph(Status.FAIL)
    colored = [
        text.plain[span.start : span.end]
        for span in text.spans
        if span.style == fail_style
    ]
    assert any(segment.startswith(fail_glyph) for segment in colored)


def test_colorized_digest_swaps_the_literal_fail_glyph_for_warns_own():
    """render_digest prefixes every non-OK block with the same literal
    "✗ ", regardless of status -- WARN's block is recolored with its own
    glyph ("!"), not left reading as a FAIL."""
    run = _mixed_status_run()
    text = _colorized_digest(run, tz=None).plain
    assert "✗ s.c · row_count" not in text
    assert "! s.c · row_count" in text
    assert "✗ s.b · null_rate" in text  # FAIL's own glyph is "✗" already
