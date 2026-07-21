"""A config may keep checks in the root config *and* in included files.
check_bearing_files() enumerates both, so the mutators that must see every
existing check -- append_checks' dedup, find_check_file, and remove_source's
orphan guard -- do not go blind to root-level checks once include: is set
(target_files, by contrast, only picks where *new* checks are written)."""

import pytest
import yaml

from dbfresh.checks import Check, check_id
from dbfresh.configurator import (
    append_checks,
    build_check,
    check_bearing_files,
    find_check_file,
    remove_check,
    remove_source,
)


def check_id_of_block(block: dict) -> str:
    return check_id(
        Check(
            source=block.get("source", ""),
            object=block.get("object", ""),
            metric=block.get("metric"),
            column=block.get("column"),
            key=block.get("key"),
        )
    )


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


def test_remove_source_orphan_guard_sees_a_root_level_check(tmp_path):
    # s is referenced only by a ROOT-level check; the included file uses s2.
    root, _ = _root_and_included(
        tmp_path,
        root_checks="- source: s\n  object: root_tbl\n  metric: row_count\n"
        "  expect: { max: 100 }\n",
        included_checks="- source: s2\n  object: incl_tbl\n  metric: row_count\n"
        "  expect: { max: 50 }\n",
    )
    before = root.read_text()
    with pytest.raises(ValueError, match="still reference source"):
        remove_source(root, "s")
    assert root.read_text() == before  # refused, wrote nothing


def test_append_checks_dedups_against_a_root_level_check(tmp_path):
    root, included = _root_and_included(
        tmp_path,
        root_checks="- source: s\n  object: root_tbl\n  metric: row_count\n"
        "  expect: { max: 100 }\n",
        included_checks="- source: s2\n  object: incl_tbl\n  metric: row_count\n"
        "  expect: { max: 50 }\n",
    )
    # A proposed block whose id collides with the ROOT check must be skipped,
    # not written to the included file (a duplicate id fails the next load).
    dup = build_check("s", "root_tbl", "row_count", expect={"max": 999})
    written, skipped = append_checks(included, [dup], config_path=root)
    assert written == 0
    assert [check_id_of_block(b) for b in skipped] == [check_id_of_block(dup)]
    assert len(yaml.safe_load(included.read_text())) == 1  # unchanged


def test_find_check_file_locates_a_root_level_check(tmp_path):
    root, _ = _root_and_included(
        tmp_path,
        root_checks="- source: s\n  object: root_tbl\n  metric: row_count\n"
        "  expect: { max: 100 }\n",
        included_checks="- source: s2\n  object: incl_tbl\n  metric: row_count\n"
        "  expect: { max: 50 }\n",
    )
    cid = check_id_of_block(
        build_check("s", "root_tbl", "row_count", expect={})
    )
    assert find_check_file(root, cid).resolve() == root.resolve()


def test_remove_check_removes_a_root_level_check(tmp_path):
    root, _ = _root_and_included(
        tmp_path,
        root_checks="- source: s\n  object: root_tbl\n  metric: row_count\n"
        "  expect: { max: 100 }\n",
        included_checks="- source: s2\n  object: incl_tbl\n  metric: row_count\n"
        "  expect: { max: 50 }\n",
    )
    cid = check_id_of_block(
        build_check("s", "root_tbl", "row_count", expect={})
    )
    remove_check(root, cid)
    # The sole root check is gone; removing the last item leaves checks:
    # empty (which YAML reads back as None), never a spurious "not found".
    data = yaml.safe_load(root.read_text())
    assert not (data.get("checks") or [])
