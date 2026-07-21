"""Config composition: splitting checks across included files."""

import os

import pytest

from dbfresh.config import ConfigError, load_config


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


_SOURCES = """
sources:
  s: { type: sqlite, database: ":memory:" }
"""


def test_include_merges_checks_from_root_and_included_file(tmp_path):
    root = _write(
        tmp_path / "config.yaml",
        _SOURCES
        + """
include:
  - checks/*.yaml
checks:
  - source: s
    object: root_table
    metric: row_count
    expect: { max: 5 }
""",
    )
    _write(
        tmp_path / "checks" / "a.yaml",
        """
checks:
  - source: s
    object: included_table
    metric: row_count
    expect: { max: 5 }
""",
    )
    cfg = load_config(root, env={})
    objects = {c.object for c in cfg.checks}
    assert objects == {"root_table", "included_table"}


def test_include_accepts_bare_list_of_checks(tmp_path):
    root = _write(
        tmp_path / "config.yaml",
        _SOURCES
        + """
include:
  - checks/*.yaml
checks: []
""",
    )
    _write(
        tmp_path / "checks" / "a.yaml",
        """
- source: s
  object: bare_list_table
  metric: row_count
  expect: { max: 5 }
""",
    )
    cfg = load_config(root, env={})
    assert [c.object for c in cfg.checks] == ["bare_list_table"]


def test_include_lexicographic_load_order(tmp_path):
    root = _write(
        tmp_path / "config.yaml",
        _SOURCES
        + """
include:
  - checks/*.yaml
checks: []
""",
    )
    _write(
        tmp_path / "checks" / "b.yaml",
        """
checks:
  - source: s
    object: from_b
    metric: row_count
    expect: { max: 5 }
""",
    )
    _write(
        tmp_path / "checks" / "a.yaml",
        """
checks:
  - source: s
    object: from_a
    metric: row_count
    expect: { max: 5 }
""",
    )
    cfg = load_config(root, env={})
    assert [c.object for c in cfg.checks] == ["from_a", "from_b"]


def test_include_glob_resolves_relative_to_root_config_dir_not_cwd(tmp_path):
    root = _write(
        tmp_path / "config.yaml",
        _SOURCES
        + """
include:
  - checks/*.yaml
checks: []
""",
    )
    _write(
        tmp_path / "checks" / "a.yaml",
        """
checks:
  - source: s
    object: included_table
    metric: row_count
    expect: { max: 5 }
""",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    cwd = os.getcwd()
    os.chdir(elsewhere)
    try:
        cfg = load_config(root, env={})
    finally:
        os.chdir(cwd)
    assert [c.object for c in cfg.checks] == ["included_table"]


def test_include_glob_matching_no_files_is_a_validation_error(tmp_path):
    root = _write(
        tmp_path / "config.yaml",
        _SOURCES
        + """
include:
  - checks/nope-*.yaml
checks: []
""",
    )
    with pytest.raises(ValueError):
        load_config(root, env={})


def test_undefined_var_in_include_pattern_names_the_variable(tmp_path):
    # An undefined ${VAR} in an include: pattern must be reported as that
    # undefined variable, not as "include glob matched no files" (the
    # literal, unresolved "${CHECKS_DIR}/*.yaml" pattern never matches).
    root = _write(
        tmp_path / "config.yaml",
        _SOURCES
        + """
include:
  - ${CHECKS_DIR}/*.yaml
checks: []
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(root, env={})
    message = str(excinfo.value)
    assert "undefined environment variable: CHECKS_DIR" in message
    assert "glob matched no files" not in message


@pytest.mark.parametrize(
    "key", ["sources", "calendar", "store", "defaults", "include"]
)
def test_included_file_with_disallowed_top_level_key_is_a_validation_error(
    tmp_path, key
):
    root = _write(
        tmp_path / "config.yaml",
        _SOURCES
        + """
include:
  - checks/*.yaml
checks: []
""",
    )
    _write(
        tmp_path / "checks" / "a.yaml",
        f"""
checks: []
{key}: {{}}
""",
    )
    with pytest.raises(ValueError):
        load_config(root, env={})


def test_duplicate_check_id_across_root_and_included_file_is_a_validation_error(
    tmp_path,
):
    root = _write(
        tmp_path / "config.yaml",
        _SOURCES
        + """
include:
  - checks/*.yaml
checks:
  - source: s
    object: t
    metric: row_count
    id: dup_id
    expect: { max: 5 }
""",
    )
    _write(
        tmp_path / "checks" / "a.yaml",
        """
checks:
  - source: s
    object: t
    metric: schema
    id: dup_id
    expect: { unchanged: true }
""",
    )
    with pytest.raises(ValueError, match="dup_id"):
        load_config(root, env={})


def test_duplicate_check_id_across_two_included_files_is_a_validation_error(
    tmp_path,
):
    root = _write(
        tmp_path / "config.yaml",
        _SOURCES
        + """
include:
  - checks/*.yaml
checks: []
""",
    )
    _write(
        tmp_path / "checks" / "a.yaml",
        """
checks:
  - source: s
    object: t
    metric: row_count
    expect: { max: 5 }
""",
    )
    _write(
        tmp_path / "checks" / "b.yaml",
        """
checks:
  - source: s
    object: t
    metric: row_count
    expect: { between: [0, 100] }
""",
    )
    with pytest.raises(ValueError):
        load_config(root, env={})


def test_no_implicit_directory_scan_only_matched_globs_load(tmp_path):
    root = _write(
        tmp_path / "config.yaml",
        _SOURCES
        + """
include:
  - checks/only-*.yaml
checks: []
""",
    )
    _write(
        tmp_path / "checks" / "only-a.yaml",
        """
checks:
  - source: s
    object: matched_table
    metric: row_count
    expect: { max: 5 }
""",
    )
    _write(
        tmp_path / "checks" / "unmatched.yaml",
        """
checks:
  - source: s
    object: unmatched_table
    metric: row_count
    expect: { max: 5 }
""",
    )
    cfg = load_config(root, env={})
    assert [c.object for c in cfg.checks] == ["matched_table"]


def test_store_path_resolves_against_root_config_dir_with_includes_present(
    tmp_path,
):
    root = _write(
        tmp_path / "config.yaml",
        _SOURCES
        + """
include:
  - checks/*.yaml
store: ./obs.db
checks: []
""",
    )
    _write(
        tmp_path / "checks" / "a.yaml",
        """
checks:
  - source: s
    object: t
    metric: row_count
    expect: { max: 5 }
""",
    )
    cwd = os.getcwd()
    os.chdir(tmp_path / "checks")
    try:
        cfg = load_config(root, env={})
    finally:
        os.chdir(cwd)
    assert cfg.config_dir == tmp_path
    assert cfg.store.path == "./obs.db"


def test_include_not_a_list_is_a_validation_error(tmp_path):
    root = _write(
        tmp_path / "config.yaml",
        _SOURCES
        + """
include: checks/a.yaml
checks: []
""",
    )
    with pytest.raises(ValueError):
        load_config(root, env={})
