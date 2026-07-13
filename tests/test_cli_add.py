"""The `dbfresh add` wizard: a thin shell over configurator.py.

The wizard's own logic is exercised through the configurator module's
tests (test_configurator_*.py); these tests only prove the CLI wiring --
prompts feed the module correctly and the result is written to disk.
"""

import yaml

from dbfresh.adapters.base import SqlAlchemyAdapter
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


def test_add_wizard_new_source_keeps_env_var_placeholder_in_yaml(tmp_path, monkeypatch):
    # A new source's connection params may reference ${VAR} secrets. The
    # probe must succeed against the resolved value, but the YAML must
    # keep the placeholder -- never the literal secret.
    db = tmp_path / "data.db"
    _table(db)
    monkeypatch.setenv("DBFRESH_TEST_DB_PATH", str(db))
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\nchecks: []\n")

    answers = iter(
        [
            "s",  # new source name
            "sqlite",  # source type
            "database=${DBFRESH_TEST_DB_PATH}",  # connection param, env-backed
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
    assert data["sources"]["s"]["database"] == "${DBFRESH_TEST_DB_PATH}"
    assert len(data["checks"]) >= 1


def test_add_wizard_hints_at_env_var_for_credential_looking_keys(
    tmp_path, monkeypatch, capsys
):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\nchecks: []\n")

    answers = iter(
        [
            "s",  # new source name
            "sqlite",  # source type
            "token=hunter2",  # a literal secret, not ${VAR}-wrapped
            "",  # end of params
            "n",  # decline adding the (unreachable) source anyway
        ]
    )
    monkeypatch.setattr("builtins.input", lambda *a: next(answers, ""))

    code = main(["add", "-c", str(cfg)])
    assert code == 1
    out = capsys.readouterr().out
    assert "${" in out


def test_add_wizard_closes_adapter_when_declining_missing_object(tmp_path, monkeypatch):
    db = tmp_path / "data.db"
    _table(db)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\nchecks: []\n')

    closed = []
    original_close = SqlAlchemyAdapter.close

    def spy_close(self):
        closed.append(self)
        return original_close(self)

    monkeypatch.setattr(SqlAlchemyAdapter, "close", spy_close)

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
    # One close from probe_connection's own adapter, one from the adapter
    # _select_source returned and _add_command must still close on the
    # early decline.
    assert len(closed) == 2


def test_prompt_number_reprompts_on_non_numeric_input(monkeypatch):
    from dbfresh.cli import _prompt_number

    answers = iter(["not-a-number", "0.1"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    assert _prompt_number("max null rate", "0.05", float) == 0.1


def test_prompt_index_reprompts_on_non_numeric_and_out_of_range(monkeypatch):
    from dbfresh.cli import _prompt_index

    answers = iter(["not-a-number", "0", "5", "2"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    assert _prompt_index("which file", "1", 3) == 1


def test_add_wizard_rejects_out_of_range_file_index(tmp_path, monkeypatch):
    db = tmp_path / "data.db"
    _table(db)
    (tmp_path / "checks").mkdir()
    (tmp_path / "checks" / "a.yaml").write_text("checks: []\n")
    (tmp_path / "checks" / "b.yaml").write_text("checks: []\n")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
        "include: [checks/*.yaml]\nchecks: []\n"
    )

    answers = iter(
        [
            "s",  # source name
            "fct",  # object name
            "y",  # accept full bundle
            "",
            "",
            "",
            "99",  # out-of-range file index
            "2",  # valid index, second included file
        ]
    )
    monkeypatch.setattr("builtins.input", lambda *a: next(answers, ""))

    code = main(["add", "-c", str(cfg)])
    assert code == 0
    data = yaml.safe_load((tmp_path / "checks" / "b.yaml").read_text())
    assert len(data["checks"]) >= 1
    data_a = yaml.safe_load((tmp_path / "checks" / "a.yaml").read_text())
    assert data_a["checks"] == []


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
