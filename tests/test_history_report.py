from zoneinfo import ZoneInfo

from dbfresh.report import render_candidates, render_history


def test_render_candidates_lists_each_match():
    candidates = [
        {
            "check_id": "aaa111222333",
            "source": "warehouse",
            "object": "dbo.fct_sales",
            "metric": "row_count",
            "label": "row_count",
        },
        {
            "check_id": "bbb444555666",
            "source": "warehouse",
            "object": "dbo.fct_sales",
            "metric": "null_rate",
            "label": "null_rate",
        },
    ]
    text = render_candidates("dbo.fct_sales", candidates)
    assert "dbo.fct_sales" in text
    assert "aaa111222333" in text
    assert "bbb444555666" in text
    assert "row_count" in text
    assert "null_rate" in text


def test_render_history_shows_header_and_rows():
    candidate = {
        "check_id": "aaa111222333",
        "source": "warehouse",
        "object": "dbo.fct_sales",
        "metric": "row_count",
        "label": "row_count",
    }
    rows = [
        {
            "observed_at": "2026-07-08T00:00:00+00:00",
            "status": "OK",
            "value": 10000.0,
            "value_text": None,
        },
        {
            "observed_at": "2026-07-09T00:00:00+00:00",
            "status": "OK",
            "value": 12000.0,
            "value_text": None,
        },
        {
            "observed_at": "2026-07-10T00:00:00+00:00",
            "status": "FAIL",
            "value": 500.0,
            "value_text": None,
        },
    ]
    text = render_history(candidate, rows)
    assert "warehouse.dbo.fct_sales" in text
    assert "aaa111222333" in text
    assert "2026-07-08T00:00:00Z" in text
    assert "10000" in text
    assert "12000" in text
    assert "FAIL" in text
    # no trend column -- value direction is redundant with status + value.
    assert "▲" not in text
    assert "▼" not in text
    assert "trend" not in text


def test_render_history_renders_rows_in_the_order_given():
    """render_history doesn't reorder ``rows`` -- it's the caller's job
    (:meth:`~dbfresh.store.Store.history` for both the CLI and the TUI) to
    hand rows in the order that should appear on screen, newest first."""
    candidate = {
        "check_id": "aaa111222333",
        "source": "warehouse",
        "object": "dbo.fct_sales",
        "metric": "row_count",
        "label": "row_count",
    }
    rows = [
        {
            "observed_at": "2026-07-10T00:00:00+00:00",
            "status": "FAIL",
            "value": 500.0,
            "value_text": None,
        },
        {
            "observed_at": "2026-07-08T00:00:00+00:00",
            "status": "OK",
            "value": 10000.0,
            "value_text": None,
        },
    ]
    text = render_history(candidate, rows)
    assert text.index("2026-07-10") < text.index("2026-07-08")


def test_render_history_row_count_shown_as_plain_integer():
    candidate = {
        "check_id": "aaa111222333",
        "source": "warehouse",
        "object": "dbo.fct_sales",
        "metric": "row_count",
        "label": "row_count",
    }
    rows = [
        {
            "observed_at": "2026-07-08T00:00:00+00:00",
            "status": "OK",
            "value": 10000.0,
            "value_text": None,
        },
    ]
    text = render_history(candidate, rows)
    assert "10000 " in text  # trailing space: no decimal, padded not truncated
    assert "10000.0" not in text


def test_render_history_null_rate_shown_as_percentage():
    candidate = {
        "check_id": "aaa111222333",
        "source": "warehouse",
        "object": "dbo.fct_sales",
        "metric": "null_rate",
        "label": "null_rate",
    }
    rows = [
        {
            "observed_at": "2026-07-08T00:00:00+00:00",
            "status": "OK",
            "value": 0.04,
            "value_text": None,
        },
    ]
    text = render_history(candidate, rows)
    assert "4.0%" in text


def test_render_history_freshness_shown_as_duration_with_reconstructed_timestamp():
    candidate = {
        "check_id": "aaa111222333",
        "source": "warehouse",
        "object": "dbo.fct_sales",
        "metric": "freshness",
        "label": "freshness",
    }
    rows = [
        {
            "observed_at": "2026-07-14T12:44:27+00:00",
            "status": "FAIL",
            "value": 464533.484447,
            "value_text": None,
        },
    ]
    text = render_history(candidate, rows)
    # Same reconstruction _format_freshness_observed does for the digest --
    # this row's own observed_at (464533.484447s before it) is the "now"
    # that produced the lag, so it stands in for run.started_at here.
    assert "5d 9h stale (last update: 2026-07-09T03:42:13Z)" in text


def test_render_history_non_freshness_rows_omit_last_update():
    candidate = {
        "check_id": "aaa111222333",
        "source": "warehouse",
        "object": "dbo.fct_sales",
        "metric": "row_count",
        "label": "row_count",
    }
    rows = [
        {
            "observed_at": "2026-07-08T00:00:00+00:00",
            "status": "OK",
            "value": 10000.0,
            "value_text": None,
        },
    ]
    text = render_history(candidate, rows)
    assert "last update" not in text


def test_render_history_uses_configured_display_timezone():
    candidate = {
        "check_id": "aaa111222333",
        "source": "warehouse",
        "object": "dbo.fct_sales",
        "metric": "row_count",
        "label": "row_count",
    }
    rows = [
        {
            "observed_at": "2026-07-08T12:00:00+00:00",
            "status": "OK",
            "value": 10000.0,
            "value_text": None,
        },
    ]
    text = render_history(candidate, rows, tz=ZoneInfo("America/New_York"))
    assert "2026-07-08T08:00:00-04:00" in text


def test_render_history_handles_no_observations():
    candidate = {
        "check_id": "aaa111222333",
        "source": "warehouse",
        "object": "dbo.fct_sales",
        "metric": "row_count",
        "label": "row_count",
    }
    text = render_history(candidate, [])
    assert "no observations" in text.lower()


def test_render_history_shows_expected_column():
    candidate = {
        "check_id": "aaa111222333",
        "source": "warehouse",
        "object": "dbo.fct_sales",
        "metric": "row_count",
        "label": "row_count",
    }
    rows = [
        {
            "observed_at": "2026-07-08T00:00:00+00:00",
            "status": "OK",
            "value": 10000.0,
            "value_text": None,
            "expected": "between 1 and 100000",
            "error": None,
        },
    ]
    text = render_history(candidate, rows)
    assert "between 1 and 100000" in text
    assert "expected" in text  # column header


def test_render_history_shows_error_text_for_an_error_row():
    candidate = {
        "check_id": "aaa111222333",
        "source": "warehouse",
        "object": "dbo.fct_sales",
        "metric": "row_count",
        "label": "row_count",
    }
    rows = [
        {
            "observed_at": "2026-07-09T00:00:00+00:00",
            "status": "ERROR",
            "value": None,
            "value_text": None,
            "expected": None,
            "error": "connection refused",
        },
    ]
    text = render_history(candidate, rows)
    assert "connection refused" in text


def test_render_history_tolerates_rows_without_expected_or_error_keys():
    """Older/hand-built row dicts (e.g. this suite's own fixtures elsewhere)
    may not carry expected/error at all -- the column still renders, just
    blank, rather than raising a KeyError."""
    candidate = {
        "check_id": "aaa111222333",
        "source": "warehouse",
        "object": "dbo.fct_sales",
        "metric": "row_count",
        "label": "row_count",
    }
    rows = [
        {"observed_at": "2026-07-08T00:00:00+00:00", "status": "OK", "value": 10000.0}
    ]
    text = render_history(candidate, rows)
    assert "10000" in text


def test_render_history_summarizes_schema_fingerprint():
    # A schema check's value lives in value_text (value is None), and the
    # full column fingerprint would dwarf the table -- so it renders as a
    # column count rather than the raw "name:type|..." string.
    candidate = {
        "check_id": "aaa111222333",
        "source": "warehouse",
        "object": "dbo.fct_sales",
        "metric": "schema",
        "label": "schema",
    }
    rows = [
        {
            "observed_at": "2026-07-10T00:00:00+00:00",
            "status": "OK",
            "value": None,
            "value_text": "id:INTEGER|email:TEXT|amount:REAL",
        },
    ]
    text = render_history(candidate, rows)
    assert "3 cols" in text
    assert "id:INTEGER" not in text


def test_summarize_fingerprint_counts_and_pluralizes():
    from dbfresh.report import _summarize_fingerprint

    assert _summarize_fingerprint("") == "0 cols"
    assert _summarize_fingerprint("id:INTEGER") == "1 col"
    assert _summarize_fingerprint("id:INTEGER|email:TEXT") == "2 cols"


def test_render_history_collapses_a_multiline_error_onto_one_row():
    """A driver error is often multi-line; it must be collapsed onto the
    observation's own row rather than spilling across new lines, or the
    fixed-width table (and the TUI History screen's one-line-per-row
    mapping) would break."""
    candidate = {
        "check_id": "aaa111222333",
        "source": "warehouse",
        "object": "dbo.fct_sales",
        "metric": "row_count",
        "label": "row_count",
    }
    rows = [
        {
            "observed_at": "2026-07-09T00:00:00+00:00",
            "status": "ERROR",
            "value": None,
            "value_text": None,
            "expected": None,
            "error": "(OperationalError) no such table: t\n[SQL: SELECT 1]\n(bg)",
        },
    ]
    lines = render_history(candidate, rows).split("\n")
    # title, blank, column header, and exactly one data row -- the error's
    # own newlines do not add lines.
    assert len(lines) == 4
    assert "no such table: t" in lines[-1]
    assert "[SQL: SELECT 1]" in lines[-1]  # detail kept, collapsed inline
