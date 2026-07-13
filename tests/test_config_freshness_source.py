import pytest

from dbfresh.config import load_config


def _write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_load_config_defaults_freshness_source_to_column(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: freshness
    column: created_at
    expect: { max_lag: 26h }
""",
    )
    cfg = load_config(path, env={})
    assert cfg.checks[0].freshness_source == "column"


def test_load_config_parses_explicit_freshness_source(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: databricks, host: h, http_path: p, token: t }
checks:
  - source: s
    object: main.gold.t
    metric: freshness
    freshness_source: describe_history
    expect: { max_lag: 26h }
""",
    )
    cfg = load_config(path, env={})
    assert cfg.checks[0].freshness_source == "describe_history"


def test_load_config_allows_omitted_column_for_describe_history(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: databricks, host: h, http_path: p, token: t }
checks:
  - source: s
    object: main.gold.t
    metric: freshness
    freshness_source: describe_history
    expect: { max_lag: 26h }
""",
    )
    cfg = load_config(path, env={})
    assert cfg.checks[0].column is None


def test_load_config_rejects_unknown_freshness_source(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: databricks, host: h, http_path: p, token: t }
checks:
  - source: s
    object: t
    metric: freshness
    freshness_source: describe_yesterday
    expect: { max_lag: 26h }
""",
    )
    with pytest.raises(ValueError):
        load_config(path, env={})


def test_load_config_requires_column_for_column_freshness_source(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: freshness
    expect: { max_lag: 26h }
""",
    )
    with pytest.raises(ValueError):
        load_config(path, env={})


def test_load_config_rejects_describe_freshness_source_on_sqlite(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: freshness
    freshness_source: describe_detail
    expect: { max_lag: 26h }
""",
    )
    with pytest.raises(ValueError):
        load_config(path, env={})


@pytest.mark.parametrize("type_", ["sqlserver", "postgres"])
def test_load_config_rejects_describe_freshness_source_on_column_only_dialects(
    tmp_path, type_
):
    path = _write(
        tmp_path,
        f"""
sources:
  s: {{ type: {type_} }}
checks:
  - source: s
    object: t
    metric: freshness
    freshness_source: describe_history
    expect: {{ max_lag: 26h }}
""",
    )
    with pytest.raises(ValueError):
        load_config(path, env={})
