"""``collect_referenced_env_vars``: the ${VAR} names a config references.

Distinct from load_config's own interpolation: this collects names for an
.env template, so it must run against an empty environment (never
os.environ) and must not fail on an undefined variable or a validation
problem -- the whole point is to run before secrets exist.
"""

import pytest

from dbfresh.config import ConfigError, collect_referenced_env_vars

_SOURCES = """
sources:
  s: { type: sqlite, database: "${DB_PATH}" }
"""


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_collect_referenced_env_vars_returns_sorted_deduped_names(tmp_path):
    root = _write(
        tmp_path / "config.yaml",
        _SOURCES
        + """
checks:
  - source: s
    object: t
    metric: row_count
    where: "region = '${REGION}'"
    expect: { max: 5 }
  - source: s
    object: t2
    metric: row_count
    where: "region = '${REGION}'"
    expect: { max: 5 }
""",
    )
    assert collect_referenced_env_vars(root) == ["DB_PATH", "REGION"]


def test_collect_referenced_env_vars_collects_var_from_included_file(tmp_path):
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
    where: "region = '${INCLUDED_VAR}'"
    expect: { max: 5 }
""",
    )
    assert collect_referenced_env_vars(root) == ["DB_PATH", "INCLUDED_VAR"]


def test_collect_referenced_env_vars_ignores_environment(tmp_path, monkeypatch):
    # The correctness guard: a var that happens to be set on the
    # generating machine must still be listed, not silently omitted.
    monkeypatch.setenv("DB_PATH", "/somewhere/real.db")
    root = _write(tmp_path / "config.yaml", _SOURCES + "checks: []\n")
    assert collect_referenced_env_vars(root) == ["DB_PATH"]


def test_collect_referenced_env_vars_with_no_vars_returns_empty_list(tmp_path):
    root = _write(
        tmp_path / "config.yaml",
        "sources:\n  s: { type: sqlite, database: ':memory:' }\nchecks: []\n",
    )
    assert collect_referenced_env_vars(root) == []


def test_collect_referenced_env_vars_missing_config_file_raises_config_error(tmp_path):
    missing = tmp_path / "does_not_exist.yaml"
    with pytest.raises(ConfigError, match="config file not found"):
        collect_referenced_env_vars(missing)


def test_collect_referenced_env_vars_skips_names_only_in_var_pattern_include(tmp_path):
    # Known limit: an include pattern that itself contains an unresolved
    # ${VAR} is never glob-resolved (same rule as the loader), so a
    # variable referenced only inside the file it would have matched is
    # not collected. The variable name in the pattern itself still is.
    root = _write(
        tmp_path / "config.yaml",
        _SOURCES
        + """
include:
  - ${CHECKS_DIR}/*.yaml
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
    where: "region = '${UNREACHABLE_VAR}'"
    expect: { max: 5 }
""",
    )
    names = collect_referenced_env_vars(root)
    assert "CHECKS_DIR" in names
    assert "UNREACHABLE_VAR" not in names
