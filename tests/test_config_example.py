"""The repo-root config.example.yaml is a copyable
starting point -- it must parse and load cleanly with its ${VAR} secrets
supplied, exercising both source types, a store block, a calendar block,
and a representative spread of checks.
"""

from pathlib import Path

from dbfresh.config import load_config

_EXAMPLE_CONFIG = Path(__file__).resolve().parent.parent / "config.example.yaml"

_ENV = {
    "MSSQL_URL": "sqlserver://reader:pw@host:1433/WarehouseDB",
    "DATABRICKS_HOST": "example.cloud.databricks.com",
    "DATABRICKS_HTTP_PATH": "/sql/1.0/warehouses/abc123",
    "DATABRICKS_TOKEN": "dummy-token",
}


def test_config_example_yaml_exists():
    assert _EXAMPLE_CONFIG.is_file()


def test_config_example_loads_cleanly():
    cfg = load_config(_EXAMPLE_CONFIG, env=_ENV)
    assert set(cfg.sources) == {"warehouse", "lakehouse"}
    assert len(cfg.checks) == 6


def test_config_example_sources_carry_timeout_and_timezone():
    cfg = load_config(_EXAMPLE_CONFIG, env=_ENV)
    warehouse = cfg.sources["warehouse"]
    assert warehouse.timeout == 30
    assert warehouse.timezone == "America/New_York"


def test_config_example_store_and_calendar_are_configured():
    cfg = load_config(_EXAMPLE_CONFIG, env=_ENV)
    assert cfg.store.path == "./dbfresh.db"
    assert cfg.calendar is not None
    assert cfg.calendar.timezone == "America/New_York"


def test_config_example_secrets_are_interpolated_not_literal():
    cfg = load_config(_EXAMPLE_CONFIG, env=_ENV)
    assert cfg.sources["warehouse"].params["url"] == _ENV["MSSQL_URL"]
    assert cfg.sources["lakehouse"].params["token"] == _ENV["DATABRICKS_TOKEN"]
