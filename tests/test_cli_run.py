import json

from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.cli import main
from dbfresh.store import Store


def _seed_db(path):
    adapter = SqliteAdapter(str(path))
    adapter.rows("CREATE TABLE t (id INTEGER)")
    adapter.rows("INSERT INTO t (id) VALUES (1), (2), (3)")
    adapter.close()


def _config(path, db, expect):
    path.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
        "checks:\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: row_count\n"
        f"    expect: {expect}\n"
    )
    return path


def test_run_all_ok_exits_zero(tmp_path, capsys):
    db = tmp_path / "data.db"
    _seed_db(db)
    cfg = _config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
    code = main(["run", "-c", str(cfg)])
    assert code == 0
    assert "1 passed" in capsys.readouterr().out


def test_run_failure_exits_two(tmp_path):
    db = tmp_path / "data.db"
    _seed_db(db)
    cfg = _config(tmp_path / "config.yaml", db, "{ max: 1 }")
    assert main(["run", "-c", str(cfg)]) == 2


def test_run_json_output(tmp_path, capsys):
    db = tmp_path / "data.db"
    _seed_db(db)
    cfg = _config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
    code = main(["run", "-c", str(cfg), "--json"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "OK"
    assert data["results"][0]["metric"] == "row_count"
    assert data["results"][0]["value"] == 3
    assert data["results"][0]["check_id"]


def test_run_persists_observations_by_default(tmp_path):
    db = tmp_path / "data.db"
    _seed_db(db)
    cfg = _config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
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


def test_run_no_store_flag_skips_persistence(tmp_path):
    db = tmp_path / "data.db"
    _seed_db(db)
    cfg = _config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
    main(["run", "-c", str(cfg), "--no-store"])
    assert not (tmp_path / "dbfresh.db").exists()


def test_run_store_flag_overrides_default_path(tmp_path):
    db = tmp_path / "data.db"
    _seed_db(db)
    cfg = _config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
    custom = tmp_path / "custom.db"
    main(["run", "-c", str(cfg), "--store", str(custom)])
    assert custom.exists()
    assert not (tmp_path / "dbfresh.db").exists()


def test_run_dbfresh_store_env_var_overrides_default(tmp_path, monkeypatch):
    db = tmp_path / "data.db"
    _seed_db(db)
    cfg = _config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
    env_path = tmp_path / "env.db"
    monkeypatch.setenv("DBFRESH_STORE", str(env_path))
    main(["run", "-c", str(cfg)])
    assert env_path.exists()
    assert not (tmp_path / "dbfresh.db").exists()


def test_run_store_flag_wins_over_env_var(tmp_path, monkeypatch):
    db = tmp_path / "data.db"
    _seed_db(db)
    cfg = _config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
    env_path = tmp_path / "env.db"
    flag_path = tmp_path / "flag.db"
    monkeypatch.setenv("DBFRESH_STORE", str(env_path))
    main(["run", "-c", str(cfg), "--store", str(flag_path)])
    assert flag_path.exists()
    assert not env_path.exists()
