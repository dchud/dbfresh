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
