from datetime import UTC, datetime

import dbfresh.runner
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


def test_run_and_persist_observations_use_injected_now_not_wall_clock(tmp_path):
    db = tmp_path / "data.db"
    _seed_db(db)
    cfg = _config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
    config = load_config(cfg)
    store = Store(tmp_path / "obs.db")
    frozen_now = datetime(2020, 1, 1, tzinfo=UTC)

    run_and_persist(config, store=store, now=frozen_now)

    row = store._conn.execute("SELECT observed_at FROM observation").fetchone()
    assert row["observed_at"] == frozen_now.isoformat()
    store.close()


def test_run_and_persist_start_run_started_at_is_injected_now(tmp_path):
    db = tmp_path / "data.db"
    _seed_db(db)
    cfg = _config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
    config = load_config(cfg)
    store = Store(tmp_path / "obs.db")
    frozen_now = datetime(2020, 1, 1, tzinfo=UTC)

    run_and_persist(config, store=store, now=frozen_now)

    row = store._conn.execute("SELECT started_at FROM run").fetchone()
    assert row["started_at"] == frozen_now.isoformat()
    store.close()


def test_run_and_persist_starts_run_row_before_evaluation(tmp_path, monkeypatch):
    db = tmp_path / "data.db"
    _seed_db(db)
    cfg = _config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
    config = load_config(cfg)
    store = Store(tmp_path / "obs.db")

    run_counts_at_evaluation = []
    original_run_checks = dbfresh.runner.run_checks

    def spy_run_checks(*args, **kwargs):
        # The run row must already exist by the time evaluation starts.
        count = store._conn.execute("SELECT COUNT(*) FROM run").fetchone()[0]
        run_counts_at_evaluation.append(count)
        return original_run_checks(*args, **kwargs)

    monkeypatch.setattr("dbfresh.runner.run_checks", spy_run_checks)

    run_and_persist(config, store=store)

    assert run_counts_at_evaluation == [1]
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


def test_run_and_persist_only_builds_adapters_for_referenced_sources(tmp_path):
    # "unused" is never referenced by a check and would fail to build --
    # run_and_persist must never even try, so it does not affect the run.
    db = tmp_path / "data.db"
    _seed_db(db)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
        "  unused: { type: does_not_exist }\n"
        "checks:\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: row_count\n"
        "    expect: { between: [1, 10] }\n"
    )
    config = load_config(cfg)

    run = run_and_persist(config, store=None)

    assert run.status == Status.OK


def test_run_and_persist_unreachable_source_is_error_others_still_run(tmp_path):
    db = tmp_path / "data.db"
    _seed_db(db)
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
    config = load_config(cfg)

    run = run_and_persist(config, store=None)

    assert run.status == Status.ERROR
    by_source = {r.source: r for r in run.results}
    assert by_source["ok"].status == Status.OK
    assert by_source["down"].status == Status.ERROR
    assert by_source["down"].error is not None


def test_run_and_persist_unreachable_source_still_persists_healthy_results(tmp_path):
    db = tmp_path / "data.db"
    _seed_db(db)
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
    config = load_config(cfg)
    store = Store(tmp_path / "obs.db")

    run_and_persist(config, store=store)

    rows = store._conn.execute(
        "SELECT source, status FROM observation ORDER BY source"
    ).fetchall()
    by_source = {row["source"]: row["status"] for row in rows}
    assert by_source == {"down": "ERROR", "ok": "OK"}
    run_row = store._conn.execute("SELECT status FROM run").fetchone()
    assert run_row["status"] == "ERROR"
    store.close()


def test_run_and_persist_closes_every_adapter_even_if_one_close_raises(
    tmp_path, monkeypatch
):
    closed = []

    class _FakeAdapter:
        def __init__(self, name):
            self.name = name
            self.dialect = None

        def scalar(self, sql):
            return 3

        def close(self):
            closed.append(self.name)
            if self.name == "bad":
                raise RuntimeError("boom on close")

    def fake_create_adapter(type_, params):
        return _FakeAdapter(type_)

    monkeypatch.setattr("dbfresh.adapters.factory.create_adapter", fake_create_adapter)

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n"
        "  bad: { type: bad }\n"
        "  good: { type: good }\n"
        "checks:\n"
        "  - source: bad\n"
        "    object: t\n"
        "    metric: row_count\n"
        "    expect: { between: [1, 10] }\n"
        "  - source: good\n"
        "    object: t\n"
        "    metric: row_count\n"
        "    expect: { between: [1, 10] }\n"
    )
    config = load_config(cfg)

    run = run_and_persist(config, store=None)

    assert run.status == Status.OK
    assert set(closed) == {"bad", "good"}
