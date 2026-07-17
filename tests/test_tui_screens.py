"""Unit tests for presentation helpers in dbfresh.tui.screens.

Full-screen rendering (including these helpers' output) is covered by the
snapshot suite in test_tui_snapshots.py; these tests check the helpers'
text/style output directly, without needing a running app.
"""

from __future__ import annotations

from dbfresh.models import Status
from dbfresh.report import render_history
from dbfresh.tui.dashboard import status_glyph, status_style
from dbfresh.tui.screens import _colorized_history

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
        span.style for span in text.spans if "FAIL" in text.plain[span.start : span.end]
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
        _row("2026-07-09T00:00:00+00:00", "ERROR", None, error="connection refused"),
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
    ok_line, skipped_line = _colorized_history(_CANDIDATE, rows, tz=None).plain.split(
        "\n"
    )[-2:]
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
        text.plain[span.start : span.end] for span in text.spans if span.style == style
    ]
    assert any(
        segment.strip().startswith(glyph) and "FAIL" in segment for segment in segments
    )
