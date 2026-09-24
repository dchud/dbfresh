"""`dbfresh show OBJECT` -- one table's lineage metadata and each of its
checks' latest status, looked up from the config and the store without
connecting to any source."""

from datetime import UTC, datetime

from helpers import write_file

from dbfresh.checks import check_id
from dbfresh.cli import main
from dbfresh.config import load_config
from dbfresh.engine import Result, Status

_CONFIG = """
sources:
  s: { type: sqlite, database: ":memory:" }
  other: { type: sqlite, database: ":memory:" }
calendar:
  timezone: UTC
tables:
  - source: s
    object: orders
    description: One row per order.
    tags: [tier1, finance]
    upstream:
      - usp_load_orders
      - name: orders_pipeline
        kind: pipeline
        url: https://example.com/orders
    downstream:
      - name: Exec Daily
        kind: dashboard
    checks:
      - metric: row_count
        expect: { between: [1, 1000] }
      - metric: null_rate
        column: email
        expect: { max: 0.05 }
      - metric: schema
        expect: { unchanged: true }
  - source: s
    object: staging_orders
    description: Landing table, not checked yet.
checks:
  - source: s
    object: customers
    metric: row_count
    expect: { min: 1 }
  - source: other
    object: customers
    metric: row_count
    expect: { min: 1 }
"""


def _setup(tmp_path, seed_observations, text=_CONFIG):
    cfg = write_file(tmp_path / "config.yaml", text)
    store = tmp_path / "obs.db"
    checks = {
        (c.source, c.object, c.metric): c
        for c in load_config(cfg, env={}).checks
    }
    when = datetime(2026, 9, 24, 9, 9, tzinfo=UTC)
    seed_observations(
        store,
        [
            (
                Result(
                    source="s",
                    object="orders",
                    metric="row_count",
                    status=Status.FAIL,
                    value=0,
                    expected="between 1 and 1000",
                    check_id=check_id(checks[("s", "orders", "row_count")]),
                ),
                when,
            ),
            (
                Result(
                    source="s",
                    object="orders",
                    metric="null_rate",
                    status=Status.ERROR,
                    error="connection\n   refused",
                    expected="max 0.05",
                    check_id=check_id(checks[("s", "orders", "null_rate")]),
                ),
                when,
            ),
        ],
    )
    return cfg, store


def _show(cfg, store, *args):
    return main(["show", *args, "-c", str(cfg), "--store", str(store)])


def test_show_prints_metadata_and_latest_statuses(
    tmp_path, capsys, seed_observations
):
    cfg, store = _setup(tmp_path, seed_observations)
    code = _show(cfg, store, "orders")
    out = capsys.readouterr().out
    # A failing check does not make the lookup fail.
    assert code == 0
    lines = out.splitlines()
    assert lines[:3] == [
        "s.orders",
        "  One row per order.",
        "  tags: tier1, finance",
    ]
    assert "upstream" in lines
    assert "  usp_load_orders" in lines
    assert "  orders_pipeline (pipeline)  https://example.com/orders" in lines
    assert "  Exec Daily (dashboard)" in lines

    checks = lines[lines.index("checks") + 1 :]
    assert len(checks) == 3
    assert checks[0].split()[:2] == ["row_count", "FAIL"]
    assert "2026-09-24" in checks[0]
    assert checks[0].endswith("0 · expected between 1 and 1000")
    # An ERROR row shows its error, collapsed to one line.
    assert checks[1].split()[:3] == ["null_rate", "(email)", "ERROR"]
    assert checks[1].endswith("connection refused")
    assert checks[2].split() == ["schema", "never", "run"]


def test_show_a_table_with_no_metadata(tmp_path, capsys, seed_observations):
    cfg, store = _setup(tmp_path, seed_observations)
    code = _show(cfg, store, "customers", "--source", "s")
    lines = capsys.readouterr().out.splitlines()
    assert code == 0
    assert lines[0] == "s.customers"
    assert lines[1].startswith("  no lineage recorded")
    assert lines[-1].split() == ["row_count", "never", "run"]


def test_show_a_metadata_only_table(tmp_path, capsys, seed_observations):
    cfg, store = _setup(tmp_path, seed_observations)
    code = _show(cfg, store, "staging_orders")
    lines = capsys.readouterr().out.splitlines()
    assert code == 0
    assert lines[:2] == [
        "s.staging_orders",
        "  Landing table, not checked yet.",
    ]
    assert lines[-2:] == ["checks", "  (no checks for this table)"]


def test_show_unknown_table_exits_one(tmp_path, capsys, seed_observations):
    cfg, store = _setup(tmp_path, seed_observations)
    assert _show(cfg, store, "nope") == 1
    assert capsys.readouterr().out.strip() == "no table 'nope' in config"
    assert _show(cfg, store, "orders", "--source", "other") == 1
    assert (
        capsys.readouterr().out.strip()
        == "no table 'orders' under source 'other' in config"
    )


def test_show_ambiguous_table_lists_candidates(
    tmp_path, capsys, seed_observations
):
    cfg, store = _setup(tmp_path, seed_observations)
    assert _show(cfg, store, "customers") == 2
    lines = capsys.readouterr().out.splitlines()
    assert "--source" in lines[0]
    assert lines[1:] == ["  s.customers", "  other.customers"]


def test_show_does_not_need_secrets(tmp_path, capsys, seed_observations):
    # A lookup never connects, so an unset ${VAR} must not stop it.
    text = _CONFIG.replace(
        '  other: { type: sqlite, database: ":memory:" }',
        '  other: { type: sqlite, database: "${DBFRESH_TEST_UNSET_SECRET}" }',
    )
    cfg, store = _setup(tmp_path, seed_observations, _CONFIG)
    write_file(cfg, text)
    assert _show(cfg, store, "orders") == 0
    assert capsys.readouterr().out.startswith("s.orders\n")


def test_show_reports_a_broken_config(tmp_path, capsys):
    cfg = write_file(tmp_path / "config.yaml", "tables: [\n")
    code = main(["show", "orders", "-c", str(cfg)])
    assert code == 3
    assert "config error" in capsys.readouterr().err
