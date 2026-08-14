"""YAML-ready check blocks (build_check) and file selection for
include:-composed configs: target_files, where a config's own checks
live, and check_bearing_files, every file that may hold them."""

import pytest

from dbfresh.configurator import (
    build_check,
    check_bearing_files,
    target_files,
)


def test_build_check_minimal_table_level():
    block = build_check("s", "t", "schema", expect={"unchanged": True})
    assert block == {
        "source": "s",
        "object": "t",
        "metric": "schema",
        "expect": {"unchanged": True},
    }


def test_build_check_column_level_includes_column_field():
    block = build_check(
        "s", "t", "null_rate", column="email", expect={"max": 0.05}
    )
    assert block["column"] == "email"
    assert "key" not in block


def test_build_check_key_level_includes_key_field():
    block = build_check(
        "s", "t", "duplicate_count", key="id", expect={"max": 0}
    )
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


def test_target_files_raises_like_load_config_on_an_unmatched_glob(tmp_path):
    # Shares dbfresh.config's resolver: an unmatched include glob is a
    # validation error here too, not a silently empty file list (which
    # the Configure screen's target_files(...)[0] would turn into an
    # IndexError).
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\ninclude: [checks/nope-*.yaml]\nchecks: []\n")
    with pytest.raises(ValueError):
        target_files(cfg)


def _root_and_included(tmp_path, *, root_checks: str, included_checks: str):
    """A root config with include:, two sources, and a root checks: block,
    plus one included checks file. Returns (root_path, included_path)."""
    (tmp_path / "checks").mkdir()
    included = tmp_path / "checks" / "a.yaml"
    included.write_text(included_checks)
    root = tmp_path / "config.yaml"
    root.write_text(
        "include:\n  - checks/*.yaml\n"
        "sources:\n"
        "  s: { type: sqlite, database: ':memory:' }\n"
        "  s2: { type: sqlite, database: ':memory:' }\n"
        f"checks:\n{root_checks}"
    )
    return root, included


def test_check_bearing_files_includes_the_root_config(tmp_path):
    root, included = _root_and_included(
        tmp_path,
        root_checks="- source: s\n  object: root_tbl\n  metric: row_count\n"
        "  expect: { max: 100 }\n",
        included_checks="- source: s2\n  object: incl_tbl\n  metric: row_count\n"
        "  expect: { max: 50 }\n",
    )
    files = {p.resolve() for p in check_bearing_files(root)}
    assert root.resolve() in files
    assert included.resolve() in files
    assert len(files) == 2


def test_check_bearing_files_is_just_the_root_without_include(tmp_path):
    root = tmp_path / "config.yaml"
    root.write_text(
        "sources:\n  s: { type: sqlite, database: ':memory:' }\nchecks: []\n"
    )
    files = [p.resolve() for p in check_bearing_files(root)]
    assert files == [root.resolve()]
