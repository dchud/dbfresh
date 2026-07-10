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
    assert "2026-07-08T00:00:00+00:00" in text
    assert "10000.0" in text
    assert "12000.0" in text
    assert "FAIL" in text
    assert "▲" in text  # 10000 -> 12000 rises
    assert "▼" in text  # 12000 -> 500 falls


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


def test_render_history_falls_back_to_value_text():
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
            "value_text": "fingerprint-xyz",
        },
    ]
    text = render_history(candidate, rows)
    assert "fingerprint-xyz" in text
