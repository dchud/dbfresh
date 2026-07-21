"""raw_source / rewrite_source / remove_source: read, edit, and delete a
configured source in the root config without ever resolving or writing a
plaintext ``${VAR}`` secret, and without disturbing any other source, check,
or comment."""

import pytest
import yaml

from dbfresh.configurator import raw_source, remove_source, rewrite_source


def test_raw_source_returns_type_and_raw_params(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n  s: { type: sqlite, database: ':memory:' }\nchecks: []\n"
    )
    type_, params = raw_source(cfg, "s")
    assert type_ == "sqlite"
    assert params == {"database": ":memory:"}


def test_raw_source_keeps_var_token_raw_not_resolved(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PASSWORD", "hunter2")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n"
        "  s: { type: sqlserver, server: db.example.com, password: "
        '"${DB_PASSWORD}" }\n'
        "checks: []\n"
    )
    type_, params = raw_source(cfg, "s")
    assert type_ == "sqlserver"
    assert params["password"] == "${DB_PASSWORD}"
    assert "hunter2" not in params.values()


def test_raw_source_raises_when_source_not_found(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\nchecks: []\n")
    with pytest.raises(ValueError):
        raw_source(cfg, "nope")


def test_rewrite_source_changes_only_target_and_preserves_others(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n"
        "  a: { type: sqlite, database: 'a.db' }\n"
        "  b: { type: sqlite, database: 'b.db' }\n"
        "checks: []\n"
    )
    rewrite_source(cfg, "a", "sqlite", {"database": "new-a.db"})

    data = yaml.safe_load(cfg.read_text())
    assert data["sources"]["a"] == {"type": "sqlite", "database": "new-a.db"}
    assert data["sources"]["b"] == {"type": "sqlite", "database": "b.db"}


def test_rewrite_source_preserves_a_comment_on_the_next_source(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n"
        "  a: { type: sqlite, database: 'a.db' }\n"
        "  # a note about b\n"
        "  b: { type: sqlite, database: 'b.db' }\n"
        "checks: []\n"
    )
    rewrite_source(cfg, "a", "sqlite", {"database": "new-a.db"})

    text = cfg.read_text()
    assert "# a note about b" in text
    data = yaml.safe_load(text)
    assert data["sources"]["a"]["database"] == "new-a.db"
    assert data["sources"]["b"]["database"] == "b.db"


def test_rewrite_source_preserves_a_var_token_param_verbatim(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n  s: { type: sqlserver, server: old-host }\nchecks: []\n"
    )
    rewrite_source(
        cfg,
        "s",
        "sqlserver",
        {"server": "new-host", "password": "${DB_PASSWORD}"},
    )

    data = yaml.safe_load(cfg.read_text())
    assert data["sources"]["s"]["password"] == "${DB_PASSWORD}"
    assert data["sources"]["s"]["server"] == "new-host"


def test_rewrite_source_round_trips_on_flow_style_sources_map(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources: {a: {type: sqlite, database: a.db}, b: {type: sqlite, "
        "database: b.db}}\nchecks: []\n"
    )
    rewrite_source(cfg, "a", "sqlite", {"database": "new-a.db"})

    data = yaml.safe_load(cfg.read_text())
    assert data["sources"]["a"]["database"] == "new-a.db"
    assert data["sources"]["b"]["database"] == "b.db"


def test_rewrite_source_raises_when_source_not_found(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\nchecks: []\n")
    with pytest.raises(ValueError):
        rewrite_source(cfg, "nope", "sqlite", {"database": "x"})
    assert yaml.safe_load(cfg.read_text())["sources"] == {}


def test_remove_source_removes_only_target_and_preserves_others(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n"
        "  a: { type: sqlite, database: 'a.db' }\n"
        "  b: { type: sqlite, database: 'b.db' }\n"
        "checks: []\n"
    )
    remove_source(cfg, "a")

    data = yaml.safe_load(cfg.read_text())
    assert set(data["sources"]) == {"b"}


def test_remove_source_preserves_a_comment_on_the_next_source(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n"
        "  a: { type: sqlite, database: 'a.db' }\n"
        "  # a note about b\n"
        "  b: { type: sqlite, database: 'b.db' }\n"
        "checks: []\n"
    )
    remove_source(cfg, "a")

    text = cfg.read_text()
    assert "# a note about b" in text
    data = yaml.safe_load(text)
    assert set(data["sources"]) == {"b"}


def test_remove_source_raises_and_writes_nothing_when_checks_reference_it(
    tmp_path,
):
    cfg = tmp_path / "config.yaml"
    original = (
        "sources:\n"
        "  s: { type: sqlite, database: ':memory:' }\n"
        "checks:\n"
        "- source: s\n"
        "  object: t\n"
        "  metric: row_count\n"
        "  expect:\n"
        "    max: 100\n"
    )
    cfg.write_text(original)

    with pytest.raises(ValueError, match="1 check"):
        remove_source(cfg, "s")

    assert cfg.read_text() == original


def test_remove_source_orphan_guard_counts_across_included_files(tmp_path):
    (tmp_path / "checks").mkdir()
    included = tmp_path / "checks" / "a.yaml"
    included.write_text(
        "checks:\n"
        "- source: s\n  object: t\n  metric: row_count\n  expect: { max: 100 }\n"
    )
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n  s: { type: sqlite, database: ':memory:' }\n"
        "include: [checks/*.yaml]\nchecks: []\n"
    )
    original = cfg.read_text()

    with pytest.raises(ValueError):
        remove_source(cfg, "s")

    assert cfg.read_text() == original


def test_remove_source_removes_last_source_leaving_sources_present_but_empty(
    tmp_path,
):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n  s: { type: sqlite, database: ':memory:' }\nchecks: []\n"
    )

    remove_source(cfg, "s")

    data = yaml.safe_load(cfg.read_text())
    assert not data.get("sources")  # {} or None both count as "empty"


def test_remove_source_raises_when_source_not_found(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\nchecks: []\n")
    with pytest.raises(ValueError):
        remove_source(cfg, "nope")
