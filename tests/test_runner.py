from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.config import load_config
from dbfresh.engine import Status
from dbfresh.runner import run_and_persist
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


def test_run_and_persist_runs_checks_and_returns_run_result(tmp_path):
    db = tmp_path / "data.db"
    _seed_db(db)
    cfg = _config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
    config = load_config(cfg)

    run = run_and_persist(config, store=None)

    assert run.status == Status.OK
    assert len(run.results) == 1
    assert run.results[0].value == 3


def test_run_and_persist_writes_observations_when_store_given(tmp_path):
    db = tmp_path / "data.db"
    _seed_db(db)
    cfg = _config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
    config = load_config(cfg)
    store = Store(tmp_path / "obs.db")

    run_and_persist(config, store=store)

    obs = store._conn.execute("SELECT status, value FROM observation").fetchone()
    assert obs["status"] == "OK"
    assert obs["value"] == 3.0
    store.close()


def test_run_and_persist_leaves_store_open_for_reuse(tmp_path):
    db = tmp_path / "data.db"
    _seed_db(db)
    cfg = _config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
    config = load_config(cfg)
    store = Store(tmp_path / "obs.db")

    run_and_persist(config, store=store)
    run_and_persist(config, store=store)  # store stays open for a second run

    count = store._conn.execute("SELECT COUNT(*) FROM run").fetchone()[0]
    assert count == 2
    store.close()


def test_run_and_persist_failure_status_reflected_in_run(tmp_path):
    db = tmp_path / "data.db"
    _seed_db(db)
    cfg = _config(tmp_path / "config.yaml", db, "{ max: 1 }")
    config = load_config(cfg)

    run = run_and_persist(config, store=None)

    assert run.status == Status.FAIL
