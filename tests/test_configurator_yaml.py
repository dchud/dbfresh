"""YAML emission: building blocks, target-file selection for
`include:`-composed configs, appending, and the re-parse/run round trip."""

import yaml

from dbfresh.adapters.factory import create_adapter
from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.config import load_config
from dbfresh.configurator import (
    add_source,
    append_checks,
    build_check,
    propose_checks,
    target_files,
)
from dbfresh.engine import Status, run_checks


def test_build_check_minimal_table_level():
    block = build_check("s", "t", "schema", expect={"unchanged": True})
    assert block == {
        "source": "s",
        "object": "t",
        "metric": "schema",
        "expect": {"unchanged": True},
    }


def test_build_check_column_level_includes_column_field():
    block = build_check("s", "t", "null_rate", column="email", expect={"max": 0.05})
    assert block["column"] == "email"
    assert "key" not in block


def test_build_check_key_level_includes_key_field():
    block = build_check("s", "t", "duplicate_count", key="id", expect={"max": 0})
    assert block["key"] == "id"
    assert "column" not in block


def test_target_files_returns_root_config_when_no_include(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\nchecks: []\n")
    assert target_files(cfg) == [cfg]


def test_target_files_returns_included_files_when_include_present(tmp_path):
    cfg = tmp_path / "config.yaml"
    (tmp_path / "checks").mkdir()
    (tmp_path / "checks" / "a.yaml").write_text("checks: []\n")
    (tmp_path / "checks" / "b.yaml").write_text("checks: []\n")
    cfg.write_text("sources: {}\ninclude: [checks/*.yaml]\nchecks: []\n")
    files = target_files(cfg)
    assert [p.name for p in files] == ["a.yaml", "b.yaml"]


def test_append_checks_to_root_config_preserves_other_keys(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n  s: { type: sqlite, database: ':memory:' }\nchecks: []\n"
    )
    new_check = {
        "source": "s",
        "object": "t",
        "metric": "row_count",
        "expect": {"max": 5},
    }
    append_checks(cfg, [new_check])
    data = yaml.safe_load(cfg.read_text())
    assert data["sources"]["s"]["type"] == "sqlite"
    assert data["checks"][0]["object"] == "t"


def test_append_checks_to_included_bare_list_file(tmp_path):
    included = tmp_path / "a.yaml"
    included.write_text(
        "- source: s\n  object: existing\n  metric: row_count\n  expect: { max: 5 }\n"
    )
    new_check = {
        "source": "s",
        "object": "new",
        "metric": "row_count",
        "expect": {"max": 5},
    }
    append_checks(included, [new_check])
    data = yaml.safe_load(included.read_text())
    assert [c["object"] for c in data] == ["existing", "new"]


def test_append_checks_to_included_mapping_file(tmp_path):
    included = tmp_path / "a.yaml"
    included.write_text(
        "checks:\n"
        "  - source: s\n    object: existing\n    metric: row_count\n"
        "    expect: { max: 5 }\n"
    )
    new_check = {
        "source": "s",
        "object": "new",
        "metric": "row_count",
        "expect": {"max": 5},
    }
    append_checks(included, [new_check])
    data = yaml.safe_load(included.read_text())
    assert [c["object"] for c in data["checks"]] == ["existing", "new"]


def test_append_checks_preserves_comments_in_root_config(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "# top comment\n"
        "sources:\n"
        "  s: { type: sqlite, database: ':memory:' }\n"
        "checks:\n"
        "  # existing check comment\n"
        "  - source: s\n"
        "    object: existing\n"
        "    metric: row_count\n"
        "    expect: { max: 5 }\n"
    )
    new_check = {
        "source": "s",
        "object": "new",
        "metric": "row_count",
        "expect": {"max": 5},
    }
    append_checks(cfg, [new_check])

    text = cfg.read_text()
    assert "# top comment" in text
    assert "# existing check comment" in text
    data = yaml.safe_load(text)
    assert [c["object"] for c in data["checks"]] == ["existing", "new"]


def test_append_checks_preserves_comments_in_included_bare_list_file(tmp_path):
    included = tmp_path / "a.yaml"
    included.write_text(
        "# a comment about this file\n"
        "- source: s\n"
        "  object: existing\n"
        "  metric: row_count\n"
        "  expect: { max: 5 }\n"
    )
    new_check = {
        "source": "s",
        "object": "new",
        "metric": "row_count",
        "expect": {"max": 5},
    }
    append_checks(included, [new_check])

    text = included.read_text()
    assert "# a comment about this file" in text
    data = yaml.safe_load(text)
    assert [c["object"] for c in data] == ["existing", "new"]


def test_append_checks_onto_empty_checks_list_preserves_comments(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\n# nothing here yet\nchecks: []\n")
    new_check = {
        "source": "s",
        "object": "new",
        "metric": "row_count",
        "expect": {"max": 5},
    }
    append_checks(cfg, [new_check])

    text = cfg.read_text()
    assert "# nothing here yet" in text
    data = yaml.safe_load(text)
    assert [c["object"] for c in data["checks"]] == ["new"]


def test_add_source_preserves_comments_in_root_config(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "# config comment\n"
        "sources:\n"
        "  existing: { type: sqlite, database: ':memory:' }\n"
        "checks: []\n"
    )
    add_source(cfg, "new", "sqlite", {"database": "other.db"})

    text = cfg.read_text()
    assert "# config comment" in text
    data = yaml.safe_load(text)
    assert set(data["sources"]) == {"existing", "new"}


def test_add_source_writes_a_new_source_into_the_root_config(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\nchecks: []\n")
    add_source(cfg, "s", "sqlite", {"database": ":memory:"})
    data = yaml.safe_load(cfg.read_text())
    assert data["sources"]["s"] == {"type": "sqlite", "database": ":memory:"}


def test_add_source_preserves_existing_sources_and_checks(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n  existing: { type: sqlite, database: ':memory:' }\n"
        "checks:\n"
        "  - source: existing\n    object: t\n    metric: row_count\n"
        "    expect: { max: 5 }\n"
    )
    add_source(cfg, "new", "sqlite", {"database": "other.db"})
    data = yaml.safe_load(cfg.read_text())
    assert set(data["sources"]) == {"existing", "new"}
    assert len(data["checks"]) == 1


def test_append_checks_skips_duplicate_check_id_and_reports_it(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\nchecks: []\n")
    guards = {"baseline": "previous", "min_ratio": 0.5, "max_ratio": 2.0}
    check = build_check("s", "t", "row_count", expect={"vs_previous": guards})
    written, skipped = append_checks(cfg, [check], config_path=cfg)
    assert written == 1
    assert skipped == []

    written, skipped = append_checks(cfg, [check], config_path=cfg)
    assert written == 0
    assert skipped == [check]

    data = yaml.safe_load(cfg.read_text())
    assert len(data["checks"]) == 1


def test_append_checks_twice_for_same_object_keeps_config_loadable(tmp_path):
    db = tmp_path / "data.db"
    adapter = SqliteAdapter(str(db))
    adapter.rows("CREATE TABLE fct (id INTEGER PRIMARY KEY, amount REAL)")
    info = adapter.describe("fct")
    proposals = propose_checks("s", "fct", info, adapter.dialect)
    adapter.close()

    cfg = tmp_path / "config.yaml"
    cfg.write_text(f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\nchecks: []\n')
    append_checks(cfg, proposals, config_path=cfg)
    append_checks(cfg, proposals, config_path=cfg)  # add run twice on the same object

    config = load_config(cfg)  # must not raise a duplicate check_id error
    assert len(config.checks) == len(proposals)


def test_append_checks_dedupes_against_other_included_files(tmp_path):
    cfg = tmp_path / "config.yaml"
    (tmp_path / "checks").mkdir()
    a = tmp_path / "checks" / "a.yaml"
    b = tmp_path / "checks" / "b.yaml"
    check = build_check("s", "t", "row_count", expect={"max": 5})
    a.write_text(yaml.safe_dump({"checks": [check]}))
    b.write_text("checks: []\n")
    cfg.write_text("sources: {}\ninclude: [checks/*.yaml]\nchecks: []\n")

    written, skipped = append_checks(b, [check], config_path=cfg)
    assert written == 0
    assert skipped == [check]
    assert yaml.safe_load(b.read_text())["checks"] == []


def test_emitted_bundle_reparses_and_runs_under_load_config(tmp_path):
    db = tmp_path / "data.db"
    adapter = SqliteAdapter(str(db))
    adapter.rows(
        "CREATE TABLE fct (id INTEGER PRIMARY KEY, amount REAL, modified_at TIMESTAMP)"
    )
    adapter.rows(
        "INSERT INTO fct (id, amount, modified_at) "
        "VALUES (1, 10.0, '2026-07-10 00:00:00')"
    )
    info = adapter.describe("fct")
    proposals = propose_checks("s", "fct", info, adapter.dialect)
    adapter.close()

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\nchecks: []\n'
    )
    append_checks(cfg_path, proposals)

    config = load_config(cfg_path)
    assert len(config.checks) == len(proposals)

    adapters = {"s": create_adapter("sqlite", {"database": str(db)})}
    run = run_checks(adapters, config.checks)
    adapters["s"].close()
    assert all(r.status != Status.ERROR for r in run.results)
