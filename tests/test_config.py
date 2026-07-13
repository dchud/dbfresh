import pytest

from dbfresh.config import ConfigError, interpolate_env, load_config


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


def test_load_config_missing_file_raises_config_error(tmp_path):
    missing = tmp_path / "does_not_exist.yaml"
    with pytest.raises(ConfigError) as excinfo:
        load_config(missing, env={})
    assert excinfo.value.__cause__ is not None


def test_load_config_invalid_yaml_raises_config_error(tmp_path):
    path = _write(tmp_path, "sources: [this is not: valid: yaml\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    assert excinfo.value.__cause__ is not None


def test_load_config_missing_object_field_raises_config_error(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    metric: row_count
    expect: { max: 5 }
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    assert isinstance(excinfo.value.__cause__, KeyError)


def test_load_config_missing_source_field_raises_config_error(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - object: t
    metric: row_count
    expect: { max: 5 }
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    assert isinstance(excinfo.value.__cause__, KeyError)


def test_load_config_missing_source_type_raises_config_error(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { database: ":memory:" }
checks: []
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    assert isinstance(excinfo.value.__cause__, KeyError)


def test_load_config_bad_expectation_raises_config_error(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: row_count
    expect: 5
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    assert isinstance(excinfo.value.__cause__, TypeError)


def test_load_config_unknown_source_ref_is_a_config_error(tmp_path):
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
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    assert isinstance(excinfo.value.__cause__, ValueError)


def test_duplicate_check_id_message_names_both_colliding_checks(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: row_count
    id: dup
    expect: { max: 5 }
  - source: s
    object: t
    metric: schema
    id: dup
    expect: { unchanged: true }
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    message = str(excinfo.value)
    assert "dup" in message
    assert "row_count" in message
    assert "schema" in message
    assert "id:" in message


def test_load_config_reports_all_missing_vars_together(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s:
    type: sqlite
    database: "${DB_PATH}"
  t:
    type: sqlite
    database: "${OTHER_PATH}"
checks:
  - source: s
    object: tbl
    metric: row_count
    where: "region = '${REGION}'"
    expect: { max: 5 }
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    message = str(excinfo.value)
    assert "DB_PATH" in message
    assert "OTHER_PATH" in message
    assert "REGION" in message


def test_load_config_single_missing_var_message_unchanged(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: "${DB_PATH}" }
checks:
  - source: s
    object: t
    metric: row_count
    expect: { max: 5 }
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    assert str(excinfo.value) == "undefined environment variable: DB_PATH"


def test_load_config_missing_var_in_included_file_accumulates_with_main(tmp_path):
    root = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: "${DB_PATH}" }
include:
  - checks/*.yaml
checks:
  - source: s
    object: root_table
    metric: row_count
    where: "region = '${REGION}'"
    expect: { max: 5 }
""",
    )
    included_dir = tmp_path / "checks"
    included_dir.mkdir()
    (included_dir / "a.yaml").write_text(
        """
checks:
  - source: s
    object: included_table
    metric: row_count
    where: "region = '${INCLUDED_VAR}'"
    expect: { max: 5 }
"""
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(root, env={})
    message = str(excinfo.value)
    assert "DB_PATH" in message
    assert "REGION" in message
    assert "INCLUDED_VAR" in message


def test_load_config_all_vars_provided_loads_successfully(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: "${DB_PATH}" }
checks:
  - source: s
    object: t
    metric: row_count
    where: "region = '${REGION}'"
    expect: { max: 5 }
""",
    )
    cfg = load_config(path, env={"DB_PATH": ":memory:", "REGION": "US"})
    assert cfg.sources["s"].params["database"] == ":memory:"
    assert cfg.checks[0].where == "region = 'US'"


def test_duplicate_check_id_differing_only_by_where_names_metric(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: row_count
    where: "region = 'US'"
    expect: { max: 100 }
  - source: s
    object: t
    metric: row_count
    where: "region = 'EU'"
    expect: { max: 100 }
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    message = str(excinfo.value)
    assert "row_count" in message
    assert "id:" in message
