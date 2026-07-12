"""The `dbfresh add` wizard: a thin shell over configurator.py.

The wizard's own logic is exercised through the configurator module's
tests (test_configurator_*.py); these tests only prove the CLI wiring --
prompts feed the module correctly and the result is written to disk.
"""

import yaml

from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.cli import main


def _table(db):
    adapter = SqliteAdapter(str(db))
    adapter.rows(
        "CREATE TABLE fct (id INTEGER PRIMARY KEY, amount REAL, modified_at TIMESTAMP)"
    )
    adapter.close()


def test_add_wizard_appends_proposed_bundle_for_existing_source(tmp_path, monkeypatch):
    db = tmp_path / "data.db"
    _table(db)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\nchecks: []\n')

    answers = iter(
        [
            "s",  # source name (existing)
            "fct",  # object name
            "y",  # accept the full proposed bundle
            "",  # skip offered checks on id
            "",  # skip offered checks on amount
            "",  # skip offered checks on modified_at
        ]
    )
    monkeypatch.setattr("builtins.input", lambda *a: next(answers, ""))

    code = main(["add", "-c", str(cfg)])
    assert code == 0

    data = yaml.safe_load(cfg.read_text())
    metrics = {c["metric"] for c in data["checks"]}
    assert {"schema", "row_count", "freshness", "duplicate_count"} <= metrics


def test_add_wizard_missing_object_requires_confirmation_to_proceed(
    tmp_path, monkeypatch
):
    db = tmp_path / "data.db"
    _table(db)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\nchecks: []\n')

    answers = iter(
        [
            "s",  # source name
            "missing_table",  # object name -- does not exist
            "n",  # decline to proceed
        ]
    )
    monkeypatch.setattr("builtins.input", lambda *a: next(answers, ""))

    code = main(["add", "-c", str(cfg)])
    assert code == 1
    data = yaml.safe_load(cfg.read_text())
    assert data["checks"] == []


def test_add_wizard_new_source_runs_connection_test(tmp_path, monkeypatch):
    db = tmp_path / "data.db"
    _table(db)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\nchecks: []\n")

    answers = iter(
        [
            "s",  # new source name
            "sqlite",  # source type
            f"database={db}",  # connection param
            "",  # end of params
            "fct",  # object name
            "y",  # accept full bundle
            "",
            "",
            "",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda *a: next(answers, ""))

    code = main(["add", "-c", str(cfg)])
    assert code == 0

    data = yaml.safe_load(cfg.read_text())
    assert data["sources"]["s"]["type"] == "sqlite"
    assert len(data["checks"]) >= 1
