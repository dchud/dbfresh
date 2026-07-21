"""Editing a config must not rewrite its line endings.

Path.read_text() collapses CRLF to LF and a plain write_text() emits LF, so
before the newline-preserving helpers editing one check in a CRLF-terminated
config rewrote every line to LF -- turning a one-line change into a whole-file
diff on a repo that keeps CRLF. Each editing entry point is covered here for a
CRLF input, over both the splice fast path and the yaml.safe_dump fallback; an
LF config must stay pure LF, and a freshly created file defaults to LF.
"""

import yaml

from dbfresh.checks import Check, check_id
from dbfresh.configurator import (
    add_source,
    append_checks,
    remove_check,
    remove_source,
    rewrite_check_expectation,
    rewrite_source,
)


def _write_crlf(path, lf_text):
    """Write ``lf_text`` to ``path`` with CRLF line endings."""
    path.write_bytes(lf_text.replace("\n", "\r\n").encode("utf-8"))


def _assert_uniform_crlf(path):
    raw = path.read_bytes()
    assert b"\r\n" in raw, "expected CRLF endings, found none"
    remainder = raw.replace(b"\r\n", b"")
    assert remainder.count(b"\n") == 0, "a line was rewritten to bare LF"
    assert remainder.count(b"\r") == 0, "a stray CR was introduced"


def _check_id_of_block(block):
    return check_id(
        Check(
            source=block.get("source", ""),
            object=block.get("object", ""),
            metric=block.get("metric"),
            column=block.get("column"),
            key=block.get("key"),
        )
    )


def test_rewrite_check_expectation_preserves_crlf(tmp_path):
    cfg = tmp_path / "config.yaml"
    _write_crlf(
        cfg,
        "sources:\n  s: { type: sqlite, database: ':memory:' }\n"
        "checks:\n"
        "- source: s\n"
        "  object: t\n"
        "  metric: row_count\n"
        "  expect:\n"
        "    max: 100\n",
    )
    cid = _check_id_of_block(
        {"source": "s", "object": "t", "metric": "row_count"}
    )

    assert rewrite_check_expectation(cfg, cid, {"max": 500}) is True

    _assert_uniform_crlf(cfg)
    assert yaml.safe_load(cfg.read_text())["checks"][0]["expect"] == {
        "max": 500
    }


def test_remove_check_preserves_crlf(tmp_path):
    cfg = tmp_path / "config.yaml"
    _write_crlf(
        cfg,
        "sources:\n  s: { type: sqlite, database: ':memory:' }\n"
        "checks:\n"
        "- source: s\n"
        "  object: t\n"
        "  metric: row_count\n"
        "  expect:\n"
        "    max: 100\n"
        "- source: s\n"
        "  object: u\n"
        "  metric: row_count\n"
        "  expect:\n"
        "    max: 100\n",
    )
    cid = _check_id_of_block(
        {"source": "s", "object": "t", "metric": "row_count"}
    )

    remove_check(cfg, cid)

    _assert_uniform_crlf(cfg)
    objects = [c["object"] for c in yaml.safe_load(cfg.read_text())["checks"]]
    assert objects == ["u"]


def test_add_source_preserves_crlf(tmp_path):
    cfg = tmp_path / "config.yaml"
    _write_crlf(
        cfg,
        "sources:\n  existing: { type: sqlite, database: ':memory:' }\nchecks: []\n",
    )

    add_source(cfg, "added", "sqlite", {"database": "other.db"})

    _assert_uniform_crlf(cfg)
    assert set(yaml.safe_load(cfg.read_text())["sources"]) == {
        "existing",
        "added",
    }


def test_append_checks_preserves_crlf_in_root_config(tmp_path):
    cfg = tmp_path / "config.yaml"
    _write_crlf(
        cfg,
        "sources:\n  s: { type: sqlite, database: ':memory:' }\nchecks: []\n",
    )

    append_checks(
        cfg,
        [
            {
                "source": "s",
                "object": "t",
                "metric": "row_count",
                "expect": {"max": 5},
            }
        ],
    )

    _assert_uniform_crlf(cfg)
    assert yaml.safe_load(cfg.read_text())["checks"][0]["object"] == "t"


def test_append_checks_preserves_crlf_in_bare_list_file(tmp_path):
    included = tmp_path / "a.yaml"
    _write_crlf(
        included,
        "- source: s\n  object: existing\n  metric: row_count\n  expect: { max: 5 }\n",
    )

    append_checks(
        included,
        [
            {
                "source": "s",
                "object": "new",
                "metric": "row_count",
                "expect": {"max": 5},
            }
        ],
    )

    _assert_uniform_crlf(included)
    assert [c["object"] for c in yaml.safe_load(included.read_text())] == [
        "existing",
        "new",
    ]


def test_rewrite_source_preserves_crlf(tmp_path):
    cfg = tmp_path / "config.yaml"
    _write_crlf(
        cfg,
        "sources:\n  s:\n    type: sqlite\n    database: ':memory:'\nchecks: []\n",
    )

    rewrite_source(cfg, "s", "sqlite", {"database": "moved.db"})

    _assert_uniform_crlf(cfg)
    assert (
        yaml.safe_load(cfg.read_text())["sources"]["s"]["database"]
        == "moved.db"
    )


def test_remove_source_preserves_crlf(tmp_path):
    cfg = tmp_path / "config.yaml"
    _write_crlf(
        cfg,
        "sources:\n"
        "  keep: { type: sqlite, database: ':memory:' }\n"
        "  drop: { type: sqlite, database: ':memory:' }\n"
        "checks: []\n",
    )

    remove_source(cfg, "drop")

    _assert_uniform_crlf(cfg)
    assert set(yaml.safe_load(cfg.read_text())["sources"]) == {"keep"}


def test_add_source_fallback_preserves_crlf(tmp_path):
    # A flow-style sources mapping can't be textually spliced, so add_source
    # falls back to a full yaml.safe_dump round trip -- which must still
    # honor the file's CRLF endings on the way out.
    cfg = tmp_path / "config.yaml"
    _write_crlf(
        cfg,
        "sources: {existing: {type: sqlite, database: ':memory:'}}\nchecks: []\n",
    )

    add_source(cfg, "added", "sqlite", {"database": "other.db"})

    _assert_uniform_crlf(cfg)
    assert set(yaml.safe_load(cfg.read_text())["sources"]) == {
        "existing",
        "added",
    }


def test_lf_config_stays_lf_after_edit(tmp_path):
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
    cid = _check_id_of_block(
        {"source": "s", "object": "t", "metric": "row_count"}
    )

    assert rewrite_check_expectation(cfg, cid, {"max": 500}) is True

    assert b"\r" not in cfg.read_bytes()


def test_add_source_to_new_file_defaults_to_lf(tmp_path):
    cfg = tmp_path / "config.yaml"  # does not exist yet

    add_source(cfg, "s", "sqlite", {"database": ":memory:"})

    assert b"\r" not in cfg.read_bytes()
    assert yaml.safe_load(cfg.read_text())["sources"]["s"]["type"] == "sqlite"
