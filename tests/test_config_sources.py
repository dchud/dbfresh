"""Source config: first-class `timezone:`/`timeout:` fields, and rejecting
a genuinely-unknown source parameter cleanly instead of a raw TypeError.
"""

import pytest

from dbfresh.config import load_config


def _write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_source_timezone_and_timeout_are_not_adapter_params(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s:
    type: sqlite
    database: ":memory:"
    timezone: America/New_York
    timeout: 30
checks: []
""",
    )
    cfg = load_config(path, env={})
    source = cfg.sources["s"]
    assert source.timezone == "America/New_York"
    assert source.timeout == 30
    assert source.params == {"database": ":memory:"}


def test_source_timezone_and_timeout_default_to_none(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks: []
""",
    )
    cfg = load_config(path, env={})
    source = cfg.sources["s"]
    assert source.timezone is None
    assert source.timeout is None


def test_unknown_source_param_is_a_clean_validation_error(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:", bogus_param: 1 }
checks: []
""",
    )
    with pytest.raises(ValueError, match="bogus_param"):
        load_config(path, env={})


def test_sqlserver_source_with_timeout_and_timezone_loads(tmp_path):
    # The spec's own example config sets timeout/timezone on a sqlserver
    # source; this must load cleanly even without pymssql installed --
    # config validation never constructs the adapter.
    path = _write(
        tmp_path,
        """
sources:
  warehouse:
    type: sqlserver
    url: sqlserver://user:pass@host/db
    timeout: 30
    timezone: America/New_York
checks: []
""",
    )
    cfg = load_config(path, env={})
    source = cfg.sources["warehouse"]
    assert source.params == {"url": "sqlserver://user:pass@host/db"}
    assert source.timeout == 30
    assert source.timezone == "America/New_York"


def test_check_source_timezone_comes_from_its_source(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s:
    type: sqlite
    database: ":memory:"
    timezone: America/New_York
checks:
  - source: s
    object: t
    metric: freshness
    column: created_at
    expect: { max_lag: 26h }
""",
    )
    cfg = load_config(path, env={})
    assert cfg.checks[0].source_timezone == "America/New_York"


def test_check_source_timezone_defaults_to_utc_when_source_has_none(tmp_path):
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
    assert cfg.checks[0].source_timezone == "UTC"


def test_unknown_source_type_is_not_flagged_here_only_at_connect_time(tmp_path):
    # A bad `type:` is a connect-time concern (create_adapter already
    # raises there); config validation must not also reject it, since an
    # unreferenced or intentionally-unreachable source must not block a
    # load that never touches it (see runner.py's failed_sources model).
    path = _write(
        tmp_path,
        """
sources:
  unused: { type: does_not_exist, some_param: 1 }
checks: []
""",
    )
    cfg = load_config(path, env={})
    assert cfg.sources["unused"].type == "does_not_exist"
