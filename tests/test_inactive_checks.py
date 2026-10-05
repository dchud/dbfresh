"""`active: false` -- pausing a check (or every check on a `tables:` entry)
without removing it from the config. A paused check runs no query and
records a SKIPPED observation that says why, so it stays visible rather
than silently disappearing, and its check_id is unchanged so pausing and
resuming keeps its history."""

import pytest
import yaml
from helpers import write_config, write_file

from dbfresh.checks import check_id
from dbfresh.cli import main
from dbfresh.config import ConfigError, load_config, validate_config
from dbfresh.engine import INACTIVE_REASON, Status
from dbfresh.runner import run_and_persist
from dbfresh.store import Store

_SOURCES = """
sources:
  s: { type: sqlite, database: ":memory:" }
"""


def _by_metric(config):
    return {c.metric: c for c in config.checks}


# -- config ---------------------------------------------------------------


def test_active_defaults_to_true_and_false_pauses_a_flat_check(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
checks:
  - source: s
    object: t
    metric: row_count
    expect: { min: 1 }
  - source: s
    object: t
    metric: schema
    expect: { unchanged: true }
    active: false
""",
    )
    checks = _by_metric(load_config(path, env={}))
    assert checks["row_count"].active is True
    assert checks["schema"].active is False


def test_a_tables_entry_pauses_inline_and_set_checks(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
check_sets:
  basic:
    checks:
      - metric: schema
        expect: { unchanged: true }
tables:
  - source: s
    object: t
    active: false
    use: basic
    checks:
      - metric: row_count
        expect: { min: 1 }
      - metric: null_rate
        column: email
        expect: { max: 0.1 }
        active: true
""",
    )
    checks = _by_metric(load_config(path, env={}))
    assert checks["schema"].active is False  # expanded from the set
    assert checks["row_count"].active is False  # inline, inherits
    assert checks["null_rate"].active is True  # its own value wins


def test_active_is_not_part_of_check_id(tmp_path):
    block = """
checks:
  - source: s
    object: t
    metric: row_count
    expect: { min: 1 }
"""
    plain = write_file(tmp_path / "plain.yaml", _SOURCES + block)
    paused = write_file(
        tmp_path / "paused.yaml", _SOURCES + block + "    active: false\n"
    )
    (a,) = load_config(plain, env={}).checks
    (b,) = load_config(paused, env={}).checks
    assert b.active is False
    assert check_id(a) == check_id(b)


def test_non_boolean_active_on_a_check_is_an_error(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
checks:
  - source: s
    object: t
    metric: row_count
    expect: { min: 1 }
    active: "no"
""",
    )
    with pytest.raises(ConfigError, match="active must be true or false"):
        load_config(path, env={})


def test_non_boolean_active_on_a_table_entry_is_reported_once(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
tables:
  - source: s
    object: t
    active: paused
    checks:
      - metric: row_count
        expect: { min: 1 }
      - metric: schema
        expect: { unchanged: true }
""",
    )
    messages = [p.message for p in validate_config(path, env={}).problems]
    assert messages == [
        "table s.t: active must be true or false, got 'paused'"
    ]


# -- running --------------------------------------------------------------


def test_a_paused_check_records_skipped_with_its_reason(
    tmp_path, seed_row_count_db
):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = write_config(
        tmp_path,
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
        "checks:\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: row_count\n"
        "    expect: { between: [1, 10] }\n"
        "  - source: s\n"
        "    object: no_such_table\n"
        "    metric: row_count\n"
        "    expect: { min: 1 }\n"
        "    active: false\n",
    )
    config = load_config(cfg, env={})
    store = Store(tmp_path / "obs.db")
    try:
        run = run_and_persist(config, store)
        by_object = {r.object: r for r in run.results}
        # The paused check never queried its missing table, so it is not
        # an ERROR, and it does not affect the run's status.
        assert by_object["no_such_table"].status == Status.SKIPPED
        assert by_object["no_such_table"].error == INACTIVE_REASON
        assert by_object["t"].status == Status.OK
        assert run.status == Status.OK

        paused = next(c for c in config.checks if not c.active)
        stored = store.latest_observation(check_id(paused))
        assert stored["status"] == "SKIPPED"
        assert stored["error"] == INACTIVE_REASON
    finally:
        store.close()


def test_a_source_with_only_paused_checks_is_never_connected(tmp_path):
    # "down" cannot be built; if the runner tried, its check would ERROR.
    cfg = write_config(
        tmp_path,
        "sources:\n"
        "  down: { type: does_not_exist }\n"
        "checks:\n"
        "  - source: down\n"
        "    object: whatever\n"
        "    metric: row_count\n"
        "    expect: { max: 5 }\n"
        "    active: false\n",
    )
    run = run_and_persist(load_config(cfg, env={}), store=None)
    (result,) = run.results
    assert result.status == Status.SKIPPED
    assert result.error == INACTIVE_REASON


def test_a_paused_check_on_an_unreachable_source_is_skipped_not_error(
    tmp_path,
):
    cfg = write_config(
        tmp_path,
        "sources:\n"
        "  down: { type: does_not_exist }\n"
        "checks:\n"
        "  - source: down\n"
        "    object: a\n"
        "    metric: row_count\n"
        "    expect: { max: 5 }\n"
        "  - source: down\n"
        "    object: b\n"
        "    metric: row_count\n"
        "    expect: { max: 5 }\n"
        "    active: false\n",
    )
    run = run_and_persist(load_config(cfg, env={}), store=None)
    by_object = {r.object: r for r in run.results}
    assert by_object["a"].status == Status.ERROR
    assert by_object["b"].status == Status.SKIPPED


# -- dbfresh show ---------------------------------------------------------


def test_show_explains_a_paused_check(tmp_path, capsys, seed_row_count_db):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = write_config(
        tmp_path,
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
        "checks:\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: row_count\n"
        "    expect: { between: [1, 10] }\n"
        "    active: false\n",
    )
    store_path = tmp_path / "obs.db"
    store = Store(store_path)
    try:
        run_and_persist(load_config(cfg, env={}), store)
    finally:
        store.close()

    assert main(["show", "t", "-c", str(cfg), "--store", str(store_path)]) == 0
    line = capsys.readouterr().out.splitlines()[-1]
    assert line.split()[:2] == ["row_count", "SKIPPED"]
    assert line.endswith(INACTIVE_REASON)


# -- config migrate -------------------------------------------------------


def _migrate(tmp_path, capsys, text):
    cfg = write_file(tmp_path / "config.yaml", _SOURCES + text)
    code = main(["config", "migrate", "-c", str(cfg)])
    captured = capsys.readouterr()
    return cfg, code, captured


def test_migrate_keeps_an_entry_level_pause_on_the_entry(tmp_path, capsys):
    _cfg, code, captured = _migrate(
        tmp_path,
        capsys,
        """
tables:
  - source: s
    object: t
    active: false
    description: Paused while the load is rebuilt.
    checks:
      - metric: row_count
        expect: { min: 1 }
""",
    )
    assert code == 0
    assert captured.out == ""
    assert "already" in captured.err.lower()


def test_migrate_moves_a_pause_onto_checks_when_flat_checks_merge_in(
    tmp_path, capsys
):
    # The flat check is not paused; folding it into a paused entry would
    # pause it, so the pause goes onto the entry's own checks instead.
    cfg, code, captured = _migrate(
        tmp_path,
        capsys,
        """
checks:
  - source: s
    object: t
    metric: schema
    expect: { unchanged: true }
tables:
  - source: s
    object: t
    active: false
    checks:
      - metric: row_count
        expect: { min: 1 }
""",
    )
    assert code == 0
    (entry,) = yaml.safe_load(captured.out)["tables"]
    assert "active" not in entry
    by_metric = {c["metric"]: c for c in entry["checks"]}
    assert by_metric["row_count"]["active"] is False
    assert "active" not in by_metric["schema"]

    migrated = write_file(tmp_path / "migrated.yaml", _SOURCES + captured.out)
    before = {check_id(c): c.active for c in load_config(cfg, env={}).checks}
    after = {
        check_id(c): c.active for c in load_config(migrated, env={}).checks
    }
    assert after == before


def test_migrate_moves_differing_entry_pauses_onto_checks(tmp_path, capsys):
    cfg, code, captured = _migrate(
        tmp_path,
        capsys,
        """
tables:
  - source: s
    object: t
    active: false
    checks:
      - metric: row_count
        expect: { min: 1 }
  - source: s
    object: t
    checks:
      - metric: schema
        expect: { unchanged: true }
""",
    )
    assert code == 0
    (entry,) = yaml.safe_load(captured.out)["tables"]
    assert "active" not in entry
    migrated = write_file(tmp_path / "migrated.yaml", _SOURCES + captured.out)
    before = {check_id(c): c.active for c in load_config(cfg, env={}).checks}
    after = {
        check_id(c): c.active for c in load_config(migrated, env={}).checks
    }
    assert after == before
    assert sorted(before.values()) == [False, True]


def test_migrate_reports_a_non_boolean_entry_pause(tmp_path, capsys):
    _cfg, code, captured = _migrate(
        tmp_path,
        capsys,
        """
tables:
  - source: s
    object: t
    active: [paused]
    checks:
      - metric: row_count
        expect: { min: 1 }
""",
    )
    assert code == 3
    assert captured.out == ""
    assert "active must be true or false" in captured.err
