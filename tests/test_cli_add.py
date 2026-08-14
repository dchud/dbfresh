"""The `dbfresh add` wizard: a thin shell over configurator.py.

The wizard's own logic is exercised through the configurator module's
tests (test_configurator_*.py); these tests only prove the CLI wiring --
prompts feed the module correctly, the proposal is emitted as YAML on
stdout, and no config file is touched.
"""

import yaml
from helpers import ambiguous_sqlite_table, sqlite_table

from dbfresh.adapters import factory
from dbfresh.adapters.base import (
    Category,
    Column,
    ObjectInfo,
    SqlAlchemyAdapter,
)
from dbfresh.adapters.databricks import DatabricksDialect
from dbfresh.cli import main


class _FakeViewAdapter:
    """A minimal adapter for a Databricks-capable view with no timestamp
    candidate -- proves ``is_view`` reaches ``propose_checks`` so no
    invalid ``describe_history`` freshness check gets proposed for it."""

    dialect = DatabricksDialect()

    def scalar(self, sql):
        return 1

    def describe(self, obj):
        column = Column(
            name="id", type="INT", nullable=False, category=Category.NUMERIC
        )
        return ObjectInfo(columns=[column], is_view=True)

    def close(self):
        pass


class _FakeKeylessAdapter:
    """A Databricks-dialect adapter with no key metadata at all -- proves the
    wizard explains why no ``duplicate_count`` was proposed rather than
    staying silent about it."""

    dialect = DatabricksDialect()

    def scalar(self, sql):
        return 1

    def describe(self, obj):
        column = Column(
            name="id", type="INT", nullable=False, category=Category.NUMERIC
        )
        return ObjectInfo(columns=[column])

    def close(self):
        pass


def _emitted(capsys):
    """The YAML document the wizard printed to stdout, parsed.

    Every prompt, warning and piece of guidance goes to stderr, so stdout
    is the emitted proposal and nothing else -- parsing it is the test that
    it stays that way.
    """
    return yaml.safe_load(capsys.readouterr().out) or {}


def test_add_wizard_emits_proposed_bundle_for_existing_source(
    tmp_path, monkeypatch, capsys
):
    db = tmp_path / "data.db"
    sqlite_table(db)
    cfg = tmp_path / "config.yaml"
    original = (
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\nchecks: []\n'
    )
    cfg.write_text(original)

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

    emitted = _emitted(capsys)
    metrics = {c["metric"] for c in emitted["checks"]}
    assert {"schema", "row_count", "freshness", "duplicate_count"} <= metrics
    assert cfg.read_text() == original  # the wizard never writes


def test_add_wizard_emits_checks_at_the_indent_they_paste_at(
    tmp_path, monkeypatch, capsys
):
    # PyYAML's default renders a sequence indentless, putting items in the
    # parent key's own column. Items in that form cannot be pasted under an
    # existing indented `checks:` -- a sequence's items must share one
    # indentation -- so the emitted block has to use the indented form.
    db = tmp_path / "data.db"
    sqlite_table(db)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\nchecks: []\n'
    )

    answers = iter(["s", "fct", "y", "", "", ""])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers, ""))

    assert main(["add", "-c", str(cfg)]) == 0

    out = capsys.readouterr().out
    assert "checks:\n  - source: s\n" in out


def test_add_wizard_run_twice_for_same_object_emits_nothing_the_second_time(
    tmp_path, monkeypatch, capsys
):
    # Pasting the first run's emission into the config is what makes the
    # second run see those checks as already defined; re-emitting them
    # would hand the user a block that load_config rejects for duplicate
    # check_ids once pasted.
    from dbfresh.config import load_config

    db = tmp_path / "data.db"
    sqlite_table(db)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n')

    def _run():
        answers = iter(["s", "fct", "y", "", "", ""])
        monkeypatch.setattr("builtins.input", lambda *a: next(answers, ""))
        return main(["add", "-c", str(cfg)])

    assert _run() == 0
    first = _emitted(capsys)
    assert first["checks"]

    cfg.write_text(cfg.read_text() + yaml.safe_dump(first))
    config = load_config(cfg)  # the pasted block is a loadable config
    assert len(config.checks) == len(first["checks"])

    assert _run() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "nothing to add" in captured.err


def test_add_wizard_dedupes_against_included_files_not_just_the_root_config(
    tmp_path, monkeypatch, capsys
):
    # Checks pasted into an *included* file must still count as defined:
    # dedup reads the whole composed config, not only the root.
    db = tmp_path / "data.db"
    sqlite_table(db)
    (tmp_path / "checks").mkdir()
    (tmp_path / "checks" / "a.yaml").write_text("checks: []\n")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
        "include: [checks/*.yaml]\n"
    )

    def _run():
        answers = iter(["s", "fct", "y", "", "", ""])
        monkeypatch.setattr("builtins.input", lambda *a: next(answers, ""))
        return main(["add", "-c", str(cfg)])

    assert _run() == 0
    first = _emitted(capsys)
    (tmp_path / "checks" / "a.yaml").write_text(yaml.safe_dump(first))

    assert _run() == 0
    assert _emitted(capsys) == {}


def test_add_wizard_dedupes_against_checks_already_defined_under_tables(
    tmp_path, monkeypatch, capsys
):
    # A check already defined under a tables: entry must count as already
    # defined too -- partition_new_checks's dedup (via
    # configurator._raw_checks_in) has to see checks nested under tables:,
    # not only the flat checks: list, or a second `add` run would
    # re-propose everything the first run already found.
    from dbfresh.config import load_config

    db = tmp_path / "data.db"
    sqlite_table(db)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n')

    def _run():
        answers = iter(["s", "fct", "y", "", "", ""])
        monkeypatch.setattr("builtins.input", lambda *a: next(answers, ""))
        return main(["add", "-c", str(cfg)])

    assert _run() == 0
    first = _emitted(capsys)
    assert first["checks"]

    grouped = {
        "tables": [
            {
                "source": "s",
                "object": "fct",
                "checks": [
                    {
                        k: v
                        for k, v in check.items()
                        if k not in ("source", "object")
                    }
                    for check in first["checks"]
                ],
            }
        ]
    }
    cfg.write_text(cfg.read_text() + yaml.safe_dump(grouped))
    config = load_config(cfg)  # the pasted grouped block is loadable
    assert len(config.checks) == len(first["checks"])

    assert _run() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "nothing to add" in captured.err


def test_add_wizard_reports_already_defined_checks_on_stderr(
    tmp_path, monkeypatch, capsys
):
    db = tmp_path / "data.db"
    sqlite_table(db)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n')

    def _run():
        answers = iter(["s", "fct", "y", "", "", ""])
        monkeypatch.setattr("builtins.input", lambda *a: next(answers, ""))
        return main(["add", "-c", str(cfg)])

    assert _run() == 0
    first = _emitted(capsys)
    # Paste back only the row_count check, so the next run has one
    # already-defined block and several new ones.
    row_count = next(c for c in first["checks"] if c["metric"] == "row_count")
    cfg.write_text(cfg.read_text() + yaml.safe_dump({"checks": [row_count]}))

    assert _run() == 0
    captured = capsys.readouterr()
    metrics = {c["metric"] for c in (yaml.safe_load(captured.out))["checks"]}
    assert "row_count" not in metrics
    assert "already defined" in captured.err


def test_add_wizard_missing_object_requires_confirmation_to_proceed(
    tmp_path, monkeypatch, capsys
):
    db = tmp_path / "data.db"
    sqlite_table(db)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\nchecks: []\n'
    )

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
    assert capsys.readouterr().out == ""


def test_add_wizard_new_source_keeps_env_var_placeholder_in_emitted_yaml(
    tmp_path, monkeypatch, capsys
):
    # A new source's connection params may reference ${VAR} secrets. The
    # probe must succeed against the resolved value, but the emitted YAML
    # must keep the placeholder -- never the literal secret.
    db = tmp_path / "data.db"
    sqlite_table(db)
    monkeypatch.setenv("DBFRESH_TEST_DB_PATH", str(db))
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\nchecks: []\n")

    answers = iter(
        [
            "s",  # new source name
            "3",  # source type (numbered menu; sqlite is #3 of the sorted set)
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

    captured = capsys.readouterr()
    emitted = yaml.safe_load(captured.out)
    assert emitted["sources"]["s"]["database"] == "${DBFRESH_TEST_DB_PATH}"
    assert str(db) not in captured.out
    assert len(emitted["checks"]) >= 1
    # The variable the pasted source will need is named for the user.
    assert "DBFRESH_TEST_DB_PATH" in captured.err


def test_add_wizard_hints_at_env_var_for_credential_looking_keys(
    tmp_path, monkeypatch, capsys
):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\nchecks: []\n")

    answers = iter(
        [
            "s",  # new source name
            "3",  # source type (numbered menu; sqlite is #3 of the sorted set)
            "token=hunter2",  # a literal secret, not ${VAR}-wrapped
            "",  # end of params
            "n",  # decline adding the (unreachable) source anyway
        ]
    )
    monkeypatch.setattr("builtins.input", lambda *a: next(answers, ""))

    code = main(["add", "-c", str(cfg)])
    assert code == 1
    assert "${" in capsys.readouterr().err


def test_add_wizard_closes_adapter_when_declining_missing_object(
    tmp_path, monkeypatch
):
    db = tmp_path / "data.db"
    sqlite_table(db)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\nchecks: []\n'
    )

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


def test_prompt_offered_check_null_rate_uses_entered_value(monkeypatch):
    from dbfresh.cli import _prompt_offered_check

    answers = iter(["0.2"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    block = _prompt_offered_check("s", "t", "email", "null_rate", False)
    assert block["expect"] == {"max": 0.2}


def test_prompt_offered_check_freshness_uses_entered_max_lag(monkeypatch):
    from dbfresh.cli import _prompt_offered_check

    answers = iter(["2h"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    block = _prompt_offered_check("s", "t", "modified_at", "freshness", False)
    assert block["expect"] == {"max_lag": "2h"}


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


def test_prompts_go_to_stderr_so_stdout_holds_only_the_emitted_yaml(
    tmp_path, monkeypatch, capsys
):
    db = tmp_path / "data.db"
    sqlite_table(db)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\nchecks: []\n'
    )

    answers = iter(["s", "fct", "y", "", "", ""])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers, ""))

    assert main(["add", "-c", str(cfg)]) == 0

    captured = capsys.readouterr()
    assert captured.out.startswith("checks:")
    assert "Object name" in captured.err
    assert "Proposed" in captured.err


def test_add_wizard_passes_is_view_so_no_freshness_is_proposed_for_a_view(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setitem(factory._ADAPTERS, "fakeview", _FakeViewAdapter)
    monkeypatch.setitem(factory._DIALECTS, "fakeview", DatabricksDialect)

    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources:\n  s: { type: fakeview }\nchecks: []\n")

    answers = iter(
        [
            "s",  # source name (existing)
            "v",  # object name (a view, no timestamp candidate)
            "y",  # accept the full proposed bundle
            "",  # skip offered checks on id
        ]
    )
    monkeypatch.setattr("builtins.input", lambda *a: next(answers, ""))

    code = main(["add", "-c", str(cfg)])
    assert code == 0

    metrics = {c["metric"] for c in _emitted(capsys)["checks"]}
    assert "freshness" not in metrics


def test_add_wizard_notes_when_engine_cannot_introspect_keys(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setitem(factory._ADAPTERS, "keyless", _FakeKeylessAdapter)
    monkeypatch.setitem(factory._DIALECTS, "keyless", DatabricksDialect)

    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources:\n  s: { type: keyless }\nchecks: []\n")

    answers = iter(
        [
            "s",  # source name (existing)
            "t",  # object name
            "y",  # accept the full proposed bundle
            "",  # skip offered checks on id
        ]
    )
    monkeypatch.setattr("builtins.input", lambda *a: next(answers, ""))

    code = main(["add", "-c", str(cfg)])
    assert code == 0
    assert "cannot introspect key" in capsys.readouterr().err


def test_add_wizard_prompts_and_uses_choice_for_ambiguous_timestamp(
    tmp_path, monkeypatch, capsys
):
    db = tmp_path / "data.db"
    ambiguous_sqlite_table(db)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\nchecks: []\n'
    )

    answers = iter(
        [
            "s",  # source name
            "fct",  # object name
            "updated_at",  # pick among the ambiguous candidates
            "y",  # accept full bundle
            "",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda *a: next(answers, ""))

    code = main(["add", "-c", str(cfg)])
    assert code == 0

    checks = _emitted(capsys)["checks"]
    freshness = next(c for c in checks if c["metric"] == "freshness")
    assert freshness["column"] == "updated_at"


def test_add_wizard_skips_freshness_when_ambiguity_prompt_left_blank(
    tmp_path, monkeypatch, capsys
):
    db = tmp_path / "data.db"
    ambiguous_sqlite_table(db)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\nchecks: []\n'
    )

    answers = iter(
        [
            "s",  # source name
            "fct",  # object name
            "",  # decline to pick -- skip freshness
            "y",  # accept full bundle
            "",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda *a: next(answers, ""))

    code = main(["add", "-c", str(cfg)])
    assert code == 0

    metrics = {c["metric"] for c in _emitted(capsys)["checks"]}
    assert "freshness" not in metrics


def test_add_wizard_new_source_runs_connection_test(
    tmp_path, monkeypatch, capsys
):
    db = tmp_path / "data.db"
    sqlite_table(db)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\nchecks: []\n")

    answers = iter(
        [
            "s",  # new source name
            "3",  # source type (numbered menu; sqlite is #3 of the sorted set)
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

    emitted = _emitted(capsys)
    assert emitted["sources"]["s"]["type"] == "sqlite"
    assert len(emitted["checks"]) >= 1


def test_add_wizard_emits_a_startable_config_when_none_exists(
    tmp_path, monkeypatch, capsys
):
    # With no config file to merge into, the emitted document has to stand
    # on its own: source and checks under their own keys, loadable as
    # written.
    from dbfresh.config import load_config

    db = tmp_path / "data.db"
    sqlite_table(db)
    cfg = tmp_path / "config.yaml"

    answers = iter(
        [
            "s",  # new source name
            "3",  # source type (sqlite)
            f"database={db}",
            "",  # end of params
            "fct",  # object name
            "y",  # accept full bundle
            "",
            "",
            "",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda *a: next(answers, ""))

    assert main(["add", "-c", str(cfg)]) == 0
    captured = capsys.readouterr()
    assert not cfg.exists()  # emitted, not written
    assert "does not exist yet" in captured.err

    cfg.write_text(captured.out)
    config = load_config(cfg)
    assert config.sources["s"].type == "sqlite"
    assert config.checks


def test_add_wizard_source_type_menu_lists_types_and_rejects_bad_choice(
    tmp_path, monkeypatch, capsys
):
    db = tmp_path / "data.db"
    sqlite_table(db)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\nchecks: []\n")

    answers = iter(
        [
            "s",  # new source name
            "9",  # out-of-range type choice -- must be rejected and re-prompted
            "3",  # then a valid choice: sqlite
            f"database={db}",
            "",  # end of params
            "fct",  # object name
            "y",  # accept the proposed bundle
            "",
            "",
            "",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda *a: next(answers, ""))

    code = main(["add", "-c", str(cfg)])
    assert code == 0

    captured = capsys.readouterr()
    for type_name in ("sqlite", "postgres", "sqlserver", "databricks"):
        assert type_name in captured.err  # the menu lists every supported type
    assert "1 to 4" in captured.err  # the out-of-range 9 was rejected
    assert yaml.safe_load(captured.out)["sources"]["s"]["type"] == "sqlite"
