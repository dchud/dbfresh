"""`dbfresh config migrate` -- group one file's checks under tables: and
print the block to paste in its place.

The round-trip test is the load-bearing one: what makes it safe to run
against a real config with real observation history is that pasting the
emitted block back in place of the original checks: produces the exact
same set of check_ids.
"""

import yaml
from helpers import write_file

from dbfresh.checks import check_id
from dbfresh.cli import main
from dbfresh.config import load_config

_SOURCES = """
sources:
  s: { type: sqlite, database: ":memory:" }
"""


def test_round_trip_preserves_check_ids(tmp_path, capsys):
    original = write_file(
        tmp_path / "config.yaml",
        _SOURCES
        + """
checks:
  - source: s
    object: orders
    metric: schema
    expect: { unchanged: true }
  - source: s
    object: orders
    metric: row_count
    expect: { max: 500 }
  - source: s
    object: customers
    metric: null_rate
    column: email
    expect: { max: 0.05 }
""",
    )
    original_ids = {check_id(c) for c in load_config(original, env={}).checks}
    assert len(original_ids) == 3

    code = main(["config", "migrate", "-c", str(original)])
    block = capsys.readouterr().out
    assert code == 0

    migrated = write_file(tmp_path / "migrated.yaml", _SOURCES + block)
    migrated_ids = {check_id(c) for c in load_config(migrated, env={}).checks}

    assert migrated_ids == original_ids


def test_migrated_block_is_shorter_than_the_checks_it_replaces(
    tmp_path, capsys
):
    # The point of grouping is a smaller file. An expectation renders
    # inline, as the example config and the docs write it -- rendered as
    # a block instead, `expect: { between: [a, b] }` alone would grow
    # from one line to four and hand back a longer file than it was given.
    checks_block = """
checks:
  - source: s
    object: orders
    metric: row_count
    expect: { between: [10000, 500000] }
  - source: s
    object: orders
    metric: freshness
    column: modified_at
    expect: { max_lag: 26h }
  - source: s
    object: orders
    metric: schema
    expect: { unchanged: true }
"""
    cfg = write_file(tmp_path / "config.yaml", _SOURCES + checks_block)

    assert main(["config", "migrate", "-c", str(cfg)]) == 0

    block = capsys.readouterr().out
    assert "expect: {between: [10000, 500000]}" in block
    before = len([line for line in checks_block.splitlines() if line.strip()])
    after = len([line for line in block.splitlines() if line.strip()])
    assert after < before


def test_every_check_field_survives_verbatim(tmp_path, capsys):
    cfg = write_file(
        tmp_path / "config.yaml",
        _SOURCES
        + """
calendar:
  timezone: America/New_York
checks:
  - source: s
    object: orders
    id: orders_row_count
    metric: row_count
    where: "region = 'US'"
    severity: warn
    by_weekday:
      mon: { max: 500 }
    on_holiday: { max: 50 }
    expect: { max: 1000 }
""",
    )

    code = main(["config", "migrate", "-c", str(cfg)])
    captured = capsys.readouterr()
    assert code == 0

    doc = yaml.safe_load(captured.out)
    assert len(doc["tables"]) == 1
    entry = doc["tables"][0]
    assert entry["source"] == "s"
    assert entry["object"] == "orders"
    assert entry["checks"] == [
        {
            "id": "orders_row_count",
            "metric": "row_count",
            "where": "region = 'US'",
            "severity": "warn",
            "by_weekday": {"mon": {"max": 500}},
            "on_holiday": {"max": 50},
            "expect": {"max": 1000},
        }
    ]


def test_entries_ordered_by_first_appearance(tmp_path, capsys):
    cfg = write_file(
        tmp_path / "config.yaml",
        _SOURCES
        + """
checks:
  - source: s
    object: zeta
    metric: row_count
    expect: { max: 5 }
  - source: s
    object: alpha
    metric: row_count
    expect: { max: 5 }
  - source: s
    object: zeta
    metric: schema
    expect: { unchanged: true }
""",
    )

    code = main(["config", "migrate", "-c", str(cfg)])
    doc = yaml.safe_load(capsys.readouterr().out)
    assert code == 0

    assert [e["object"] for e in doc["tables"]] == ["zeta", "alpha"]
    zeta = doc["tables"][0]
    assert [c["metric"] for c in zeta["checks"]] == ["row_count", "schema"]


def test_partially_migrated_file_yields_one_entry_per_pair(tmp_path, capsys):
    cfg = write_file(
        tmp_path / "config.yaml",
        _SOURCES
        + """
tables:
  - source: s
    object: orders
    checks:
      - metric: schema
        expect: { unchanged: true }
checks:
  - source: s
    object: orders
    metric: row_count
    expect: { max: 5 }
  - source: s
    object: customers
    metric: null_rate
    column: email
    expect: { max: 0.05 }
""",
    )

    code = main(["config", "migrate", "-c", str(cfg)])
    doc = yaml.safe_load(capsys.readouterr().out)
    assert code == 0

    objects = [e["object"] for e in doc["tables"]]
    assert objects.count("orders") == 1
    assert set(objects) == {"orders", "customers"}
    orders = next(e for e in doc["tables"] if e["object"] == "orders")
    assert [c["metric"] for c in orders["checks"]] == ["schema", "row_count"]


def test_already_fully_grouped_file_emits_nothing(tmp_path, capsys):
    cfg = write_file(
        tmp_path / "config.yaml",
        _SOURCES
        + """
tables:
  - source: s
    object: orders
    checks:
      - metric: row_count
        expect: { max: 5 }
""",
    )

    code = main(["config", "migrate", "-c", str(cfg)])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out == ""
    assert "already" in captured.err.lower()


def test_file_with_no_checks_emits_nothing(tmp_path, capsys):
    cfg = write_file(tmp_path / "config.yaml", _SOURCES + "checks: []\n")

    code = main(["config", "migrate", "-c", str(cfg)])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out == ""
    assert "no checks" in captured.err.lower()


def test_include_narrates_each_included_file_by_name(tmp_path, capsys):
    root = write_file(
        tmp_path / "config.yaml",
        _SOURCES
        + """
include:
  - checks/*.yaml
checks: []
""",
    )
    write_file(tmp_path / "checks" / "a.yaml", "checks: []\n")
    write_file(tmp_path / "checks" / "b.yaml", "checks: []\n")

    code = main(["config", "migrate", "-c", str(root)])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out == ""
    assert "a.yaml" in captured.err
    assert "b.yaml" in captured.err
    assert "its own" in captured.err


def test_include_files_own_checks_are_not_migrated_by_the_root_run(
    tmp_path, capsys
):
    # migrate operates on the one file -c resolves to, never the composed
    # config: checks living only in an included file must not appear in
    # the root run's emitted block.
    root = write_file(
        tmp_path / "config.yaml",
        _SOURCES
        + """
include:
  - checks/*.yaml
checks: []
""",
    )
    write_file(
        tmp_path / "checks" / "a.yaml",
        """
checks:
  - source: s
    object: included_table
    metric: row_count
    expect: { max: 5 }
""",
    )

    code = main(["config", "migrate", "-c", str(root)])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out == ""
    assert "included_table" not in captured.err


def test_stdout_carries_only_yaml_narration_on_stderr(tmp_path, capsys):
    cfg = write_file(
        tmp_path / "config.yaml",
        _SOURCES
        + """
checks:
  - source: s
    object: orders
    metric: row_count
    expect: { max: 5 }
""",
    )

    code = main(["config", "migrate", "-c", str(cfg)])
    captured = capsys.readouterr()

    assert code == 0
    doc = yaml.safe_load(captured.out)
    assert set(doc) == {"tables"}
    assert "not carried over" in captured.err
    assert "untouched" in captured.err
    assert "not carried over" not in captured.out
    assert "untouched" not in captured.out


def test_set_backed_table_entry_is_preserved_verbatim(tmp_path, capsys):
    # A tables: entry that uses a check set must survive migrate
    # untouched, keeping its use:/with:/skip: -- expanding it here would
    # bake the set's checks into the file as literal blocks, silently
    # undoing the factoring and making the file bigger, the opposite of
    # what migrate is for.
    cfg = write_file(
        tmp_path / "config.yaml",
        _SOURCES
        + """
check_sets:
  standard:
    checks:
      - metric: schema
        expect: { unchanged: true }
      - metric: freshness
        column: "{{ ts_column }}"
        expect: { max_lag: 24h }

tables:
  - source: s
    object: orders
    use: standard
    with: { ts_column: modified_at }
    skip: [freshness]
checks:
  - source: s
    object: customers
    metric: row_count
    expect: { max: 5 }
""",
    )

    code = main(["config", "migrate", "-c", str(cfg)])
    captured = capsys.readouterr()
    doc = yaml.safe_load(captured.out)
    assert code == 0
    assert "carried over unchanged" in captured.err

    set_backed = next(e for e in doc["tables"] if e.get("use") == "standard")
    assert set_backed == {
        "source": "s",
        "object": "orders",
        "use": "standard",
        "with": {"ts_column": "modified_at"},
        "skip": ["freshness"],
    }
    flat_entry = next(e for e in doc["tables"] if e["object"] == "customers")
    assert flat_entry["checks"][0]["metric"] == "row_count"

    # A table's parameters render inline, the way the docs write them. A
    # block-style copy costs a line per parameter on every table in the
    # file, in output whose whole purpose is a smaller one.
    assert "with: {ts_column: modified_at}" in captured.out


def test_already_grouped_file_with_a_set_backed_entry_needs_no_migration(
    tmp_path, capsys
):
    cfg = write_file(
        tmp_path / "config.yaml",
        _SOURCES
        + """
check_sets:
  standard:
    checks:
      - metric: schema
        expect: { unchanged: true }

tables:
  - source: s
    object: customers
    checks:
      - metric: row_count
        expect: { max: 5 }
  - source: s
    object: orders
    use: standard
""",
    )

    code = main(["config", "migrate", "-c", str(cfg)])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out == ""
    assert "already" in captured.err.lower()


def test_malformed_table_entry_is_a_config_error(tmp_path, capsys):
    cfg = write_file(
        tmp_path / "config.yaml",
        _SOURCES
        + """
tables:
  - source: s
    object: orders
    tags: [oops]
    checks:
      - metric: row_count
        expect: { max: 5 }
""",
    )

    code = main(["config", "migrate", "-c", str(cfg)])
    captured = capsys.readouterr()

    assert code == 3
    assert captured.out == ""
    assert "tags" in captured.err
