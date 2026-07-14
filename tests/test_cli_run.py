import contextlib
import json
import re

from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.cli import main
from dbfresh.store import Store


def test_run_all_ok_exits_zero(tmp_path, capsys, seed_row_count_db, row_count_config):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = row_count_config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
    code = main(["run", "-c", str(cfg)])
    assert code == 0
    assert "1 passed" in capsys.readouterr().out


def test_run_failure_exits_two(tmp_path, seed_row_count_db, row_count_config):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = row_count_config(tmp_path / "config.yaml", db, "{ max: 1 }")
    assert main(["run", "-c", str(cfg)]) == 2


def test_run_json_output(tmp_path, capsys, seed_row_count_db, row_count_config):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = row_count_config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
    code = main(["run", "-c", str(cfg), "--json"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "OK"
    assert data["results"][0]["metric"] == "row_count"
    assert data["results"][0]["value"] == 3
    assert data["results"][0]["check_id"]


def test_run_json_envelope_has_run_metadata_and_counts(
    tmp_path, capsys, seed_row_count_db, row_count_config
):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = row_count_config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
    main(["run", "-c", str(cfg), "--json"])
    data = json.loads(capsys.readouterr().out)

    assert isinstance(data["run_id"], int)
    assert re.search(r"T\d{2}:\d{2}:\d{2}Z$", data["started_at"])
    assert re.search(r"T\d{2}:\d{2}:\d{2}Z$", data["finished_at"])
    assert data["counts"] == {
        "OK": 1,
        "WARN": 0,
        "FAIL": 0,
        "ERROR": 0,
        "SKIPPED": 0,
    }


def test_run_json_envelope_run_id_null_under_no_store(
    tmp_path, capsys, seed_row_count_db, row_count_config
):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = row_count_config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
    main(["run", "-c", str(cfg), "--json", "--no-store"])
    data = json.loads(capsys.readouterr().out)

    assert data["run_id"] is None
    assert data["started_at"] is not None
    assert data["finished_at"] is not None


def test_run_json_envelope_metric_check_shape(tmp_path, capsys, seed_row_count_db):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
        "checks:\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: null_rate\n"
        "    column: id\n"
        "    expect: { max: 0.5 }\n"
    )
    main(["run", "-c", str(cfg), "--json"])
    result = json.loads(capsys.readouterr().out)["results"][0]

    assert result["label"] is None
    assert result["tier"] == "column"
    assert result["value"] == 0.0
    assert result["value_text"] is None
    assert result["observed"] == "0.0"
    assert result["expected"] == "max 0.5"
    assert result["diff"] is None
    assert result["error"] is None
    assert result["samples"] is None


def test_run_json_envelope_schema_check_shape(tmp_path, capsys):
    db = tmp_path / "data.db"
    adapter = SqliteAdapter(str(db))
    adapter.rows("CREATE TABLE t (id INTEGER, name TEXT)")
    adapter.close()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
        "checks:\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: schema\n"
        "    expect: { unchanged: true }\n"
    )
    main(["run", "-c", str(cfg), "--json"])  # first run: establishes baseline
    capsys.readouterr()  # discard the baseline run's output

    adapter = SqliteAdapter(str(db))
    adapter.rows("ALTER TABLE t ADD COLUMN email TEXT")
    adapter.close()

    main(["run", "-c", str(cfg), "--json"])
    result = json.loads(capsys.readouterr().out)["results"][0]

    assert result["metric"] == "schema"
    assert result["tier"] == "table"
    assert result["status"] == "FAIL"
    assert result["value"] is None
    assert result["value_text"] == result["observed"]
    assert "email:TEXT" in result["value_text"]
    assert result["diff"] == ["+ email (TEXT)"]


def test_run_json_envelope_assertion_shape(tmp_path, capsys):
    db = tmp_path / "data.db"
    adapter = SqliteAdapter(str(db))
    adapter.rows("CREATE TABLE fct (amount REAL)")
    adapter.rows("INSERT INTO fct VALUES (10.0), (-5.0)")
    adapter.close()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
        "checks:\n"
        "  - source: s\n"
        "    object: fct\n"
        "    assert: amount >= 0\n"
    )
    main(["run", "-c", str(cfg), "--json"])
    result = json.loads(capsys.readouterr().out)["results"][0]

    assert result["metric"] is None
    assert result["label"] == "assert amount >= 0"
    assert result["tier"] == "table"
    assert result["status"] == "FAIL"
    assert result["value"] == 1.0
    assert result["value_text"] is None
    assert result["observed"] == "1"
    assert len(result["samples"]) == 1


def test_run_persists_observations_by_default(
    tmp_path, seed_row_count_db, row_count_config
):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = row_count_config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
    main(["run", "-c", str(cfg)])

    store_path = tmp_path / "dbfresh.db"
    assert store_path.exists()
    store = Store(store_path)
    obs = store._conn.execute(
        "SELECT check_id, status, value, source, object, metric FROM observation"
    ).fetchone()
    assert obs["status"] == "OK"
    assert obs["value"] == 3.0
    assert obs["source"] == "s"
    assert obs["object"] == "t"
    assert obs["metric"] == "row_count"
    assert len(obs["check_id"]) == 12  # derived id, no explicit id: set

    run_row = store._conn.execute("SELECT status, git_sha FROM run").fetchone()
    assert run_row["status"] == "OK"
    assert run_row["git_sha"] is None  # tmp_path config dir is not a git repo
    store.close()


def test_run_no_store_flag_skips_persistence(
    tmp_path, seed_row_count_db, row_count_config
):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = row_count_config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
    main(["run", "-c", str(cfg), "--no-store"])
    assert not (tmp_path / "dbfresh.db").exists()


def test_run_store_flag_overrides_default_path(
    tmp_path, seed_row_count_db, row_count_config
):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = row_count_config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
    custom = tmp_path / "custom.db"
    main(["run", "-c", str(cfg), "--store", str(custom)])
    assert custom.exists()
    assert not (tmp_path / "dbfresh.db").exists()


def test_run_dbfresh_store_env_var_overrides_default(
    tmp_path, monkeypatch, seed_row_count_db, row_count_config
):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = row_count_config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
    env_path = tmp_path / "env.db"
    monkeypatch.setenv("DBFRESH_STORE", str(env_path))
    main(["run", "-c", str(cfg)])
    assert env_path.exists()
    assert not (tmp_path / "dbfresh.db").exists()


def test_run_skips_check_off_schedule(tmp_path, capsys, seed_row_count_db):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
        "calendar:\n"
        "  timezone: UTC\n"
        "  workdays: []\n"  # no day is ever a business day
        "checks:\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: row_count\n"
        "    expect: { between: [1, 10] }\n"
        "    skip_off_schedule: true\n"
    )
    code = main(["run", "-c", str(cfg)])
    assert code == 0
    assert "1 skipped" in capsys.readouterr().out


def test_run_digest_header_uses_calendar_timezone(tmp_path, capsys, seed_row_count_db):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
        "calendar:\n"
        "  timezone: America/New_York\n"
        "checks:\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: row_count\n"
        "    expect: { between: [1, 10] }\n"
    )
    code = main(["run", "-c", str(cfg)])
    assert code == 0
    header = capsys.readouterr().out.splitlines()[0]
    assert "DATA CHECK REPORT — " in header
    assert re.search(r"T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$", header)


def test_run_schema_check_establishes_baseline_then_detects_drift(tmp_path, capsys):
    db = tmp_path / "data.db"
    adapter = SqliteAdapter(str(db))
    adapter.rows("CREATE TABLE t (id INTEGER, name TEXT)")
    adapter.close()

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
        "checks:\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: schema\n"
        "    expect: { unchanged: true }\n"
    )
    assert main(["run", "-c", str(cfg)]) == 0

    adapter = SqliteAdapter(str(db))
    adapter.rows("ALTER TABLE t ADD COLUMN email TEXT")
    adapter.close()

    assert main(["run", "-c", str(cfg)]) == 2
    out = capsys.readouterr().out
    assert "+ email (TEXT)" in out


def test_run_schema_check_no_store_always_passes(tmp_path):
    db = tmp_path / "data.db"
    adapter = SqliteAdapter(str(db))
    adapter.rows("CREATE TABLE t (id INTEGER)")
    adapter.close()

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
        "checks:\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: schema\n"
        "    expect: { unchanged: true }\n"
    )
    assert main(["run", "-c", str(cfg), "--no-store"]) == 0

    adapter = SqliteAdapter(str(db))
    adapter.rows("ALTER TABLE t ADD COLUMN extra TEXT")
    adapter.close()

    assert main(["run", "-c", str(cfg), "--no-store"]) == 0


def test_run_store_flag_wins_over_env_var(
    tmp_path, monkeypatch, seed_row_count_db, row_count_config
):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = row_count_config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
    env_path = tmp_path / "env.db"
    flag_path = tmp_path / "flag.db"
    monkeypatch.setenv("DBFRESH_STORE", str(env_path))
    main(["run", "-c", str(cfg), "--store", str(flag_path)])
    assert flag_path.exists()
    assert not env_path.exists()


_VS_PREVIOUS_EXPECT = (
    "{ vs_previous: { baseline: previous, min_ratio: 0.5, max_ratio: 2.0 } }"
)


def _insert_n_rows(db, n, start=0):
    adapter = SqliteAdapter(str(db))
    values = ", ".join(f"({i})" for i in range(start, start + n))
    adapter.rows(f"INSERT INTO t (id) VALUES {values}")
    adapter.close()


def test_run_vs_previous_establishes_baseline_then_detects_3x_swing(
    tmp_path, row_count_config
):
    db = tmp_path / "data.db"
    adapter = SqliteAdapter(str(db))
    adapter.rows("CREATE TABLE t (id INTEGER)")
    adapter.close()
    _insert_n_rows(db, 100)

    cfg = row_count_config(tmp_path / "config.yaml", db, _VS_PREVIOUS_EXPECT)
    assert main(["run", "-c", str(cfg)]) == 0  # first run: no baseline, on_missing pass

    _insert_n_rows(db, 250, start=100)  # table now has 350 rows

    assert main(["run", "-c", str(cfg)]) == 2  # 350 vs baseline 100 -> 3.5x swing


def test_run_vs_previous_no_store_always_on_missing_pass(
    tmp_path, seed_row_count_db, row_count_config
):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = row_count_config(tmp_path / "config.yaml", db, _VS_PREVIOUS_EXPECT)
    assert main(["run", "-c", str(cfg), "--no-store"]) == 0
    assert main(["run", "-c", str(cfg), "--no-store"]) == 0


def test_run_unsupported_metric_is_a_clean_config_error(
    tmp_path, capsys, seed_row_count_db
):
    # An unknown metric is validated at config-load time -- a clean
    # config error naming it, not a mid-run crash on one check's result.
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
        "checks:\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: row_count\n"
        "    expect: { between: [1, 10] }\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: not_a_real_metric\n"
        "    expect: { max: 5 }\n"
    )

    code = main(["run", "-c", str(cfg), "--json"])

    captured = capsys.readouterr()
    assert code == 3
    assert "unknown metric: 'not_a_real_metric'" in captured.err
    assert captured.out == ""


def _only_config(tmp_path, db):
    # "down" would fail to build were it ever touched.
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'sources:\n  ok: {{ type: sqlite, database: "{db}" }}\n'
        "  down: { type: does_not_exist }\n"
        "checks:\n"
        "  - source: ok\n"
        "    object: t\n"
        "    metric: row_count\n"
        "    expect: { between: [1, 10] }\n"
        "  - source: down\n"
        "    object: whatever\n"
        "    metric: row_count\n"
        "    expect: { max: 5 }\n"
    )
    return cfg


def test_run_only_flag_restricts_to_one_source(tmp_path, seed_row_count_db):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = _only_config(tmp_path, db)
    assert main(["run", "-c", str(cfg), "--only", "ok"]) == 0


def test_run_only_unknown_source_is_a_clean_error(
    tmp_path, capsys, seed_row_count_db, row_count_config
):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = row_count_config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
    code = main(["run", "-c", str(cfg), "--only", "nope"])
    assert code == 3
    assert "nope" in capsys.readouterr().err


def test_run_no_progress_flag_is_accepted(
    tmp_path, capsys, seed_row_count_db, row_count_config
):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = row_count_config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
    code = main(["run", "-c", str(cfg), "--no-progress"])
    assert code == 0
    assert "1 passed" in capsys.readouterr().out


def test_run_command_derives_show_progress_from_json_and_no_progress_flags(
    tmp_path, monkeypatch, seed_row_count_db, row_count_config
):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = row_count_config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
    seen = {}

    def fake_show_progress(json_output, no_progress, stream=None):
        seen["json_output"] = json_output
        seen["no_progress"] = no_progress
        return False

    monkeypatch.setattr("dbfresh.report.show_progress", fake_show_progress)

    main(["run", "-c", str(cfg), "--no-progress"])

    assert seen == {"json_output": False, "no_progress": True}


def test_run_command_sizes_progress_by_the_only_filtered_check_count(
    tmp_path, monkeypatch, seed_row_count_db
):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = _only_config(tmp_path, db)
    captured = {}

    @contextlib.contextmanager
    def fake_progress_reporter(total, enabled, console=None):
        captured["total"] = total
        captured["enabled"] = enabled
        yield lambda result: None

    monkeypatch.setattr("dbfresh.report.progress_reporter", fake_progress_reporter)

    code = main(["run", "-c", str(cfg), "--only", "ok"])

    assert code == 0
    assert captured["total"] == 1
