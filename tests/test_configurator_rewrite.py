"""rewrite_check_expectation: edit an already-written check's expect: value
in place, and find_check_file: locate which target_files() file holds it."""

import yaml

from dbfresh.checks import Check, check_id
from dbfresh.configurator import build_check, find_check_file, rewrite_check_expectation


def test_rewrite_check_expectation_splices_block_style_in_place(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n  s: { type: sqlite, database: ':memory:' }\n"
        "checks:\n"
        "- source: s\n"
        "  object: t\n"
        "  metric: freshness\n"
        "  column: modified_at\n"
        "  expect:\n"
        "    max_lag: 24h\n"
    )
    check = build_check(
        "s", "t", "freshness", column="modified_at", expect={"max_lag": "24h"}
    )
    cid = check_id_of_block(check)

    ok = rewrite_check_expectation(cfg, cid, {"max_lag": "48h"})

    assert ok is True
    data = yaml.safe_load(cfg.read_text())
    assert data["checks"][0]["expect"] == {"max_lag": "48h"}
    assert data["sources"]["s"]["type"] == "sqlite"  # untouched


def test_rewrite_check_expectation_preserves_comments_and_other_checks(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "# top comment\n"
        "sources:\n  s: { type: sqlite, database: ':memory:' }\n"
        "checks:\n"
        "- source: s\n"
        "  object: t\n"
        "  metric: row_count\n"
        "  # a comment on this check\n"
        "  expect:\n"
        "    max: 100\n"
        "- source: s\n"
        "  object: t\n"
        "  metric: null_rate\n"
        "  column: email\n"
        "  expect:\n"
        "    max: 0.05\n"
    )
    row_count_check = build_check("s", "t", "row_count", expect={"max": 100})
    cid = check_id_of_block(row_count_check)

    ok = rewrite_check_expectation(cfg, cid, {"max": 500})

    assert ok is True
    text = cfg.read_text()
    assert "# top comment" in text
    assert "# a comment on this check" in text
    data = yaml.safe_load(text)
    assert data["checks"][0]["expect"] == {"max": 500}
    assert data["checks"][1]["expect"] == {"max": 0.05}  # untouched


def test_rewrite_check_expectation_falls_back_to_round_trip_for_inline_flow_style(
    tmp_path,
):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n  s: { type: sqlite, database: ':memory:' }\n"
        "checks:\n"
        "- source: s\n  object: t\n  metric: row_count\n  expect: { max: 100 }\n"
    )
    check = build_check("s", "t", "row_count", expect={"max": 100})
    cid = check_id_of_block(check)

    ok = rewrite_check_expectation(cfg, cid, {"max": 500})

    assert ok is True
    data = yaml.safe_load(cfg.read_text())
    assert data["checks"][0]["expect"] == {"max": 500}


def test_rewrite_check_expectation_works_on_bare_list_included_file(tmp_path):
    included = tmp_path / "a.yaml"
    included.write_text(
        "- source: s\n  object: t\n  metric: row_count\n  expect:\n    max: 100\n"
    )
    check = build_check("s", "t", "row_count", expect={"max": 100})
    cid = check_id_of_block(check)

    ok = rewrite_check_expectation(included, cid, {"max": 999})

    assert ok is True
    data = yaml.safe_load(included.read_text())
    assert data[0]["expect"] == {"max": 999}


def test_rewrite_check_expectation_returns_false_when_check_id_not_found(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\nchecks: []\n")

    ok = rewrite_check_expectation(cfg, "nonexistent", {"max": 1})

    assert ok is False


def test_rewrite_check_expectation_does_not_change_check_id(tmp_path):
    # check_id deliberately excludes expect from its hash -- editing a
    # threshold must never fork history.
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n  s: { type: sqlite, database: ':memory:' }\n"
        "checks:\n- source: s\n  object: t\n  metric: row_count\n"
        "  expect:\n    max: 100\n"
    )
    check = build_check("s", "t", "row_count", expect={"max": 100})
    cid = check_id_of_block(check)

    rewrite_check_expectation(cfg, cid, {"max": 999})

    data = yaml.safe_load(cfg.read_text())
    rewritten = build_check("s", "t", "row_count", expect=data["checks"][0]["expect"])
    assert check_id_of_block(rewritten) == cid


def test_rewrite_check_expectation_writes_a_between_lo_hi_pair(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n  s: { type: sqlite, database: ':memory:' }\n"
        "checks:\n"
        "- source: s\n"
        "  object: t\n"
        "  metric: row_count\n"
        "  expect:\n"
        "    between: [1, 1000]\n"
    )
    check = build_check("s", "t", "row_count", expect={"between": [1, 1000]})
    cid = check_id_of_block(check)

    ok = rewrite_check_expectation(cfg, cid, {"between": [5, 2000]})

    assert ok is True
    data = yaml.safe_load(cfg.read_text())
    assert data["checks"][0]["expect"] == {"between": [5, 2000]}
    assert data["sources"]["s"]["type"] == "sqlite"  # untouched


def test_rewrite_check_expectation_between_preserves_other_checks(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n  s: { type: sqlite, database: ':memory:' }\n"
        "checks:\n"
        "- source: s\n"
        "  object: t\n"
        "  metric: row_count\n"
        "  expect:\n"
        "    between: [1, 1000]\n"
        "- source: s\n"
        "  object: t\n"
        "  metric: null_rate\n"
        "  column: email\n"
        "  expect:\n"
        "    max: 0.05\n"
    )
    row_count_check = build_check("s", "t", "row_count", expect={"between": [1, 1000]})
    cid = check_id_of_block(row_count_check)

    ok = rewrite_check_expectation(cfg, cid, {"between": [10, 500]})

    assert ok is True
    data = yaml.safe_load(cfg.read_text())
    assert data["checks"][0]["expect"] == {"between": [10, 500]}
    assert data["checks"][1]["expect"] == {"max": 0.05}  # untouched


def test_rewrite_check_expectation_between_works_on_included_file(tmp_path):
    (tmp_path / "checks").mkdir()
    a = tmp_path / "checks" / "a.yaml"
    a.write_text(
        "checks:\n"
        "- source: s\n"
        "  object: t\n"
        "  metric: row_count\n"
        "  expect:\n"
        "    between: [1, 1000]\n"
    )
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\ninclude: [checks/*.yaml]\nchecks: []\n")
    check = build_check("s", "t", "row_count", expect={"between": [1, 1000]})
    cid = check_id_of_block(check)

    target = find_check_file(cfg, cid)
    assert target == a
    ok = rewrite_check_expectation(target, cid, {"between": [2, 3000]})

    assert ok is True
    data = yaml.safe_load(a.read_text())
    assert data["checks"][0]["expect"] == {"between": [2, 3000]}


def test_find_check_file_locates_the_file_containing_a_check_id(tmp_path):
    (tmp_path / "checks").mkdir()
    a = tmp_path / "checks" / "a.yaml"
    b = tmp_path / "checks" / "b.yaml"
    check = build_check("s", "t", "row_count", expect={"max": 5})
    a.write_text(yaml.safe_dump({"checks": [check]}))
    b.write_text("checks: []\n")
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\ninclude: [checks/*.yaml]\nchecks: []\n")

    found = find_check_file(cfg, check_id_of_block(check))

    assert found == a


def test_find_check_file_returns_none_when_not_found(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\nchecks: []\n")

    assert find_check_file(cfg, "nonexistent") is None


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
