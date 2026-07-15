"""remove_check: delete a single already-written check by check_id, across
single- and multi-file configs, without disturbing any other check."""

import pytest
import yaml

from dbfresh.checks import Check, check_id
from dbfresh.configurator import build_check, remove_check


def check_id_of_block(block: dict) -> str:
    """A raw check block's derived check_id, via the real Check dataclass --
    mirrors what configurator.py's own _check_id_of does internally, kept
    independent here so a test bug in one doesn't mask one in the other."""
    return check_id(
        Check(
            source=block.get("source", ""),
            object=block.get("object", ""),
            metric=block.get("metric"),
            column=block.get("column"),
            key=block.get("key"),
        )
    )


def test_remove_check_deletes_target_and_preserves_others(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n  s: { type: sqlite, database: ':memory:' }\n"
        "checks:\n"
        "- source: s\n"
        "  object: t\n"
        "  metric: row_count\n"
        "  expect:\n"
        "    max: 100\n"
        "- source: s\n"
        "  object: t\n"
        "  metric: null_rate\n"
        "  column: email\n"
        "  expect:\n"
        "    max: 0.05\n"
    )
    row_count = build_check("s", "t", "row_count", expect={"max": 100})
    null_rate = build_check("s", "t", "null_rate", column="email", expect={"max": 0.05})

    remove_check(cfg, check_id_of_block(row_count))

    data = yaml.safe_load(cfg.read_text())
    assert len(data["checks"]) == 1
    assert data["checks"][0]["metric"] == "null_rate"
    assert data["checks"][0]["expect"] == {"max": 0.05}
    assert check_id_of_block(data["checks"][0]) == check_id_of_block(null_rate)
    assert data["sources"]["s"]["type"] == "sqlite"  # untouched


def test_remove_check_preserves_comments_on_other_checks(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "# top comment\n"
        "sources:\n  s: { type: sqlite, database: ':memory:' }\n"
        "checks:\n"
        "- source: s\n"
        "  object: t\n"
        "  metric: row_count\n"
        "  expect:\n"
        "    max: 100\n"
        "- source: s\n"
        "  object: t\n"
        "  metric: null_rate\n"
        "  column: email\n"
        "  # a comment on the surviving check\n"
        "  expect:\n"
        "    max: 0.05\n"
    )
    row_count = build_check("s", "t", "row_count", expect={"max": 100})

    remove_check(cfg, check_id_of_block(row_count))

    text = cfg.read_text()
    assert "# top comment" in text
    assert "# a comment on the surviving check" in text
    data = yaml.safe_load(text)
    assert len(data["checks"]) == 1
    assert data["checks"][0]["metric"] == "null_rate"


def test_remove_check_across_multi_file_include_only_touches_owning_file(tmp_path):
    (tmp_path / "checks").mkdir()
    a = tmp_path / "checks" / "a.yaml"
    b = tmp_path / "checks" / "b.yaml"
    in_a = build_check("s", "t", "row_count", expect={"max": 100})
    in_b = build_check("s", "u", "row_count", expect={"max": 50})
    a.write_text(yaml.safe_dump({"checks": [in_a]}, sort_keys=False))
    b.write_text(yaml.safe_dump({"checks": [in_b]}, sort_keys=False))
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\ninclude: [checks/*.yaml]\nchecks: []\n")
    b_text_before = b.read_text()

    remove_check(cfg, check_id_of_block(in_a))

    data_a = yaml.safe_load(a.read_text())
    assert not data_a.get("checks")  # empty (None or []) either way is valid
    assert b.read_text() == b_text_before  # other file is byte-for-byte untouched


def test_remove_check_handles_last_check_leaves_valid_empty_checks_file(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n  s: { type: sqlite, database: ':memory:' }\n"
        "checks:\n"
        "- source: s\n"
        "  object: t\n"
        "  metric: row_count\n"
        "  expect:\n"
        "    max: 100\n"
    )
    check = build_check("s", "t", "row_count", expect={"max": 100})

    remove_check(cfg, check_id_of_block(check))

    data = yaml.safe_load(cfg.read_text())
    assert data["sources"]["s"]["type"] == "sqlite"  # untouched
    assert not data.get("checks")  # empty (None or []) either way is valid


def test_remove_check_handles_last_check_in_bare_list_included_file(tmp_path):
    included = tmp_path / "checks" / "a.yaml"
    included.parent.mkdir()
    check = build_check("s", "t", "row_count", expect={"max": 100})
    included.write_text(yaml.safe_dump([check], sort_keys=False))
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\ninclude: [checks/*.yaml]\nchecks: []\n")

    remove_check(cfg, check_id_of_block(check))

    data = yaml.safe_load(included.read_text())
    assert not data  # empty file (None) parses to an empty check list


def test_remove_check_raises_when_check_id_not_found(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n  s: { type: sqlite, database: ':memory:' }\n"
        "checks:\n"
        "- source: s\n"
        "  object: t\n"
        "  metric: row_count\n"
        "  expect:\n"
        "    max: 100\n"
    )

    with pytest.raises(ValueError):
        remove_check(cfg, "nonexistent")

    # nothing was touched
    data = yaml.safe_load(cfg.read_text())
    assert len(data["checks"]) == 1


def test_remove_check_raises_on_empty_config_rather_than_no_op(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\nchecks: []\n")

    with pytest.raises(ValueError):
        remove_check(cfg, "nonexistent")


def test_remove_check_falls_back_to_round_trip_for_inline_flow_style(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n  s: { type: sqlite, database: ':memory:' }\n"
        "checks:\n"
        "- source: s\n  object: t\n  metric: row_count\n  expect: { max: 100 }\n"
        "- source: s\n  object: t\n  metric: null_rate\n  column: email\n"
        "  expect: { max: 0.05 }\n"
    )
    row_count = build_check("s", "t", "row_count", expect={"max": 100})

    remove_check(cfg, check_id_of_block(row_count))

    data = yaml.safe_load(cfg.read_text())
    assert len(data["checks"]) == 1
    assert data["checks"][0]["metric"] == "null_rate"


def test_remove_check_does_not_remove_a_leading_comment_meant_for_the_next_check(
    tmp_path,
):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n  s: { type: sqlite, database: ':memory:' }\n"
        "checks:\n"
        "- source: s\n"
        "  object: t\n"
        "  metric: row_count\n"
        "  expect:\n"
        "    max: 100\n"
        "# a note about the next check\n"
        "- source: s\n"
        "  object: t\n"
        "  metric: null_rate\n"
        "  column: email\n"
        "  expect:\n"
        "    max: 0.05\n"
    )
    row_count = build_check("s", "t", "row_count", expect={"max": 100})

    remove_check(cfg, check_id_of_block(row_count))

    text = cfg.read_text()
    assert "# a note about the next check" in text
    data = yaml.safe_load(text)
    assert len(data["checks"]) == 1
    assert data["checks"][0]["metric"] == "null_rate"
