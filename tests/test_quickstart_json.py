"""The quickstart's --json example must match what render_json emits.

Extracts the fenced ```json block from docs/quickstart.md and compares its
shape against a real run of the equivalent scenario -- same sources,
objects, and checks the prose describes -- so the doc never silently drifts
from the actual contract (a missing/renamed key, a wrong tier, ...).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.cli import main

_QUICKSTART = Path(__file__).parent.parent / "docs" / "quickstart.md"


def _doc_json_example() -> dict:
    text = _QUICKSTART.read_text()
    blocks = re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)
    (block,) = blocks
    return json.loads(block)


def _run_quickstart_scenario(tmp_path, capsys) -> dict:
    db = tmp_path / "demo.db"
    adapter = SqliteAdapter(str(db))
    adapter.rows("CREATE TABLE orders (id INTEGER, customer_email TEXT)")
    adapter.rows(
        "INSERT INTO orders (id, customer_email) VALUES "
        "(1, 'a@example.com'), (2, 'b@example.com'), (3, NULL)"
    )
    adapter.close()

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'sources:\n  demo: {{ type: sqlite, database: "{db}" }}\n'
        "checks:\n"
        "  - source: demo\n"
        "    object: orders\n"
        "    metric: row_count\n"
        "    expect: { between: [1, 1000000] }\n"
        "  - source: demo\n"
        "    object: orders\n"
        "    metric: null_rate\n"
        "    column: customer_email\n"
        "    expect: { max: 0.5 }\n"
    )
    main(["run", "-c", str(cfg), "--json"])
    return json.loads(capsys.readouterr().out)


def test_quickstart_json_example_has_the_same_top_level_keys(tmp_path, capsys):
    doc = _doc_json_example()
    actual = _run_quickstart_scenario(tmp_path, capsys)
    assert set(doc) == set(actual)


def test_quickstart_json_example_results_have_the_same_keys(tmp_path, capsys):
    doc = _doc_json_example()
    actual = _run_quickstart_scenario(tmp_path, capsys)
    assert len(doc["results"]) == len(actual["results"])
    pairs = zip(doc["results"], actual["results"], strict=True)
    for doc_result, actual_result in pairs:
        assert set(doc_result) == set(actual_result)


def test_quickstart_json_example_matches_the_deterministic_fields(
    tmp_path, capsys
):
    doc = _doc_json_example()
    actual = _run_quickstart_scenario(tmp_path, capsys)
    assert doc["status"] == actual["status"]

    by_metric = {r["metric"]: r for r in actual["results"]}
    for expected in doc["results"]:
        result = by_metric[expected["metric"]]
        assert result["check_id"] == expected["check_id"]
        assert result["source"] == expected["source"]
        assert result["object"] == expected["object"]
        assert result["label"] == expected["label"]
        assert result["tier"] == expected["tier"]
        assert result["status"] == expected["status"]
        assert result["expected"] == expected["expected"]
        assert result["value_text"] == expected["value_text"]
        assert result["error"] == expected["error"]
        assert result["samples"] == expected["samples"]
        assert result["diff"] == expected["diff"]
