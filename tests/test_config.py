import pytest

from dbfresh.config import interpolate_env, load_config


def test_interpolate_env_replaces_var():
    assert interpolate_env("${FOO}", {"FOO": "bar"}) == "bar"
    assert interpolate_env("a${FOO}b", {"FOO": "x"}) == "axb"


def test_interpolate_env_missing_var_raises():
    with pytest.raises(ValueError):
        interpolate_env("${MISSING}", {})


def test_interpolate_env_recurses_into_containers():
    out = interpolate_env({"url": "${U}", "items": ["${U}", 3]}, {"U": "z"})
    assert out == {"url": "z", "items": ["z", 3]}


def _write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_load_config_builds_sources_and_checks(tmp_path):
    path = _write(
        tmp_path,
        """
version: 1
sources:
  s:
    type: sqlite
    database: ":memory:"
checks:
  - source: s
    object: t
    metric: row_count
    expect: { between: [1, 10] }
""",
    )
    cfg = load_config(path, env={})
    assert cfg.sources["s"].type == "sqlite"
    assert cfg.sources["s"].params == {"database": ":memory:"}
    assert len(cfg.checks) == 1
    assert cfg.checks[0].metric == "row_count"
    assert cfg.checks[0].expect.evaluate(5) is True


def test_load_config_rejects_unknown_source_ref(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: other
    object: t
    metric: row_count
    expect: { max: 5 }
""",
    )
    with pytest.raises(ValueError):
        load_config(path, env={})


def test_load_config_exposes_config_dir(tmp_path):
    path = _write(tmp_path, "sources: {}\nchecks: []\n")
    cfg = load_config(path, env={})
    assert cfg.config_dir == tmp_path


def test_load_config_defaults_store_to_none(tmp_path):
    path = _write(tmp_path, "sources: {}\nchecks: []\n")
    cfg = load_config(path, env={})
    assert cfg.store is None


def test_load_config_bare_string_store_is_path_shorthand(tmp_path):
    path = _write(tmp_path, "store: ./obs.db\nsources: {}\nchecks: []\n")
    cfg = load_config(path, env={})
    assert cfg.store.path == "./obs.db"
    assert cfg.store.retain_days == 400


def test_load_config_store_mapping_with_retain_days(tmp_path):
    path = _write(
        tmp_path,
        "store: { path: ./obs.db, retain_days: 90 }\nsources: {}\nchecks: []\n",
    )
    cfg = load_config(path, env={})
    assert cfg.store.path == "./obs.db"
    assert cfg.store.retain_days == 90


def test_load_config_store_mapping_without_path(tmp_path):
    path = _write(tmp_path, "store: { retain_days: 10 }\nsources: {}\nchecks: []\n")
    cfg = load_config(path, env={})
    assert cfg.store.path is None
    assert cfg.store.retain_days == 10


def test_load_config_schema_check_accepts_unchanged(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: schema
    expect: { unchanged: true }
""",
    )
    cfg = load_config(path, env={})
    assert cfg.checks[0].expect.operator == "unchanged"


def test_load_config_schema_check_rejects_numeric_operator(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: schema
    expect: { max: 5 }
""",
    )
    with pytest.raises(ValueError):
        load_config(path, env={})


def test_load_config_rejects_unchanged_on_non_schema_check(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: row_count
    expect: { unchanged: true }
""",
    )
    with pytest.raises(ValueError):
        load_config(path, env={})
