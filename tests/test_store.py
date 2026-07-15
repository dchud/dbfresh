import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.checks import Check, parse_expectation
from dbfresh.config import StoreConfig
from dbfresh.engine import Result, Status, evaluate_check
from dbfresh.store import Store, capture_git_sha, resolve_store_path


def _result(**overrides) -> Result:
    fields = {
        "object": "dbo.fct_sales",
        "metric": "row_count",
        "status": Status.OK,
        "source": "warehouse",
        "value": 42,
        "check_id": "abc123def456",
    }
    fields.update(overrides)
    return Result(**fields)


def test_store_creates_run_and_observation_tables(tmp_path):
    store = Store(tmp_path / "obs.db")
    conn = sqlite3.connect(str(tmp_path / "obs.db"))
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"run", "observation"} <= tables
    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "ix_obs_checkid_time" in indexes
    conn.close()
    store.close()


def test_store_open_sets_wal_journal_mode(tmp_path):
    store = Store(tmp_path / "obs.db")
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
    store.close()


def test_store_open_sets_busy_timeout(tmp_path):
    store = Store(tmp_path / "obs.db")
    timeout = store._conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout == 5000
    store.close()


def test_start_run_records_started_at_and_git_sha(tmp_path):
    store = Store(tmp_path / "obs.db")
    before = datetime.now(UTC)
    run_id = store.start_run(git_sha="deadbeef")
    assert isinstance(run_id, int)
    row = store._conn.execute(
        "SELECT started_at, git_sha, finished_at FROM run WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    started_at = datetime.fromisoformat(row[0])
    assert started_at >= before
    assert row[1] == "deadbeef"
    assert row[2] is None
    store.close()


def test_start_run_git_sha_defaults_to_none(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    row = store._conn.execute(
        "SELECT git_sha FROM run WHERE run_id = ?", (run_id,)
    ).fetchone()
    assert row[0] is None
    store.close()


def test_finish_run_sets_finished_at_and_status(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    store.finish_run(run_id, Status.FAIL)
    row = store._conn.execute(
        "SELECT finished_at, status FROM run WHERE run_id = ?", (run_id,)
    ).fetchone()
    assert row[0] is not None
    assert row[1] == "FAIL"
    store.close()


def test_record_observation_round_trips_numeric_value(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    observed_at = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)  # a Friday
    store.record_observation(run_id, _result(value=42), observed_at=observed_at)
    row = store._conn.execute(
        "SELECT run_id, check_id, source, object, metric, label, value, "
        "value_text, status, observed_at, weekday FROM observation"
    ).fetchone()
    assert row[0] == run_id
    assert row[1] == "abc123def456"
    assert row[2] == "warehouse"
    assert row[3] == "dbo.fct_sales"
    assert row[4] == "row_count"
    assert row[5] == "row_count"  # falls back to metric when label is unset
    assert row[6] == 42.0
    assert row[7] is None
    assert row[8] == "OK"
    assert row[9] == observed_at.isoformat()
    assert row[10] == 4  # Friday
    store.close()


def test_record_observation_stores_non_numeric_value_as_text(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    store.record_observation(run_id, _result(value="fingerprint-xyz"))
    row = store._conn.execute("SELECT value, value_text FROM observation").fetchone()
    assert row[0] is None
    assert row[1] == "fingerprint-xyz"
    store.close()


def test_record_observation_persists_error_status_with_no_value(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    store.record_observation(
        run_id, _result(status=Status.ERROR, value=None, error="boom")
    )
    row = store._conn.execute(
        "SELECT status, value, value_text FROM observation"
    ).fetchone()
    assert row[0] == "ERROR"
    assert row[1] is None
    assert row[2] is None
    store.close()


def test_record_observations_persists_every_result(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    results = [_result(check_id="a", value=1), _result(check_id="b", value=2)]
    store.record_observations(run_id, results)
    rows = store._conn.execute(
        "SELECT check_id, value FROM observation ORDER BY check_id"
    ).fetchall()
    assert [(row["check_id"], row["value"]) for row in rows] == [
        ("a", 1.0),
        ("b", 2.0),
    ]
    store.close()


def test_record_observations_is_a_single_transaction_not_one_per_row(tmp_path):
    # If a mid-batch failure still leaves an earlier row durable, each row
    # was committed on its own; a single transaction discards all of them
    # together once the connection closes without ever reaching commit().
    db_path = tmp_path / "obs.db"
    store = Store(db_path)
    run_id = store.start_run()
    good = _result(check_id="a", value=1)
    bad = _result(check_id="b", value=2)
    bad.status = "NOT_A_STATUS"  # not a valid Status -> raises mid-batch

    with pytest.raises(ValueError):
        store.record_observations(run_id, [good, bad])
    store.close()  # uncommitted writes are discarded here

    reopened = Store(db_path)
    count = reopened._conn.execute("SELECT COUNT(*) FROM observation").fetchone()[0]
    assert count == 0
    reopened.close()


def test_record_observation_uses_explicit_label_for_assertions(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    store.record_observation(
        run_id,
        _result(metric=None, label="assert amount >= 0", value=3),
    )
    row = store._conn.execute("SELECT metric, label FROM observation").fetchone()
    assert row[0] is None
    assert row[1] == "assert amount >= 0"
    store.close()


def test_history_returns_recent_observations_oldest_first(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    for day, value in [(1, 10), (2, 20), (3, 30)]:
        store.record_observation(
            run_id,
            _result(value=value),
            observed_at=datetime(2026, 7, day, tzinfo=UTC),
        )
    rows = store.history("abc123def456", limit=30)
    assert [r["value"] for r in rows] == [10.0, 20.0, 30.0]
    store.close()


def test_history_limits_to_n_most_recent(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    for day, value in [(1, 10), (2, 20), (3, 30)]:
        store.record_observation(
            run_id,
            _result(value=value),
            observed_at=datetime(2026, 7, day, tzinfo=UTC),
        )
    rows = store.history("abc123def456", limit=2)
    assert [r["value"] for r in rows] == [20.0, 30.0]
    store.close()


def test_history_filters_by_check_id(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    store.record_observation(run_id, _result(check_id="aaa", value=1))
    store.record_observation(run_id, _result(check_id="bbb", value=2))
    rows = store.history("aaa")
    assert len(rows) == 1
    assert rows[0]["value"] == 1.0
    store.close()


def test_prune_removes_observations_older_than_retain_days(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    now = datetime(2026, 7, 10, tzinfo=UTC)
    store.record_observation(
        run_id, _result(check_id="old", value=1), observed_at=now.replace(year=2025)
    )
    store.record_observation(run_id, _result(check_id="new", value=2), observed_at=now)
    store.prune(retain_days=30, now=now)
    remaining = {
        row[0]
        for row in store._conn.execute("SELECT check_id FROM observation").fetchall()
    }
    assert remaining == {"new"}
    store.close()


def test_prune_removes_orphaned_old_runs(tmp_path):
    store = Store(tmp_path / "obs.db")
    now = datetime(2026, 7, 10, tzinfo=UTC)
    old_run = store.start_run(started_at=now.replace(year=2025))
    store.finish_run(old_run, Status.OK, finished_at=now.replace(year=2025))
    store.record_observation(
        old_run,
        _result(check_id="old", value=1),
        observed_at=now.replace(year=2025),
    )
    store.prune(retain_days=30, now=now)
    remaining_runs = store._conn.execute("SELECT run_id FROM run").fetchall()
    assert remaining_runs == []
    store.close()


def test_capture_git_sha_inside_repo_matches_head(tmp_path):
    repo_dir = Path(__file__).resolve().parent.parent
    expected = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert capture_git_sha(repo_dir) == expected


def test_capture_git_sha_outside_repo_is_none(tmp_path):
    assert capture_git_sha(tmp_path) is None


def test_resolve_store_path_cli_flag_wins(tmp_path):
    path = resolve_store_path(
        config_dir=tmp_path,
        store_config=StoreConfig(path="./configured.db"),
        cli_store="./from-flag.db",
        env_store="./from-env.db",
    )
    assert path == Path("./from-flag.db")


def test_resolve_store_path_env_var_wins_over_config(tmp_path):
    path = resolve_store_path(
        config_dir=tmp_path,
        store_config=StoreConfig(path="./configured.db"),
        cli_store=None,
        env_store="./from-env.db",
    )
    assert path == Path("./from-env.db")


def test_resolve_store_path_config_relative_path_resolves_against_config_dir(
    tmp_path,
):
    path = resolve_store_path(
        config_dir=tmp_path,
        store_config=StoreConfig(path="./obs.db"),
    )
    assert path == tmp_path / "obs.db"


def test_resolve_store_path_config_absolute_path_used_verbatim(tmp_path):
    absolute = tmp_path / "elsewhere" / "obs.db"
    path = resolve_store_path(
        config_dir=tmp_path,
        store_config=StoreConfig(path=str(absolute)),
    )
    assert path == absolute


def test_resolve_store_path_default_resolves_against_config_dir(tmp_path):
    path = resolve_store_path(config_dir=tmp_path, store_config=None)
    assert path == tmp_path / "dbfresh.db"


def test_resolve_store_path_default_used_when_config_has_no_path(tmp_path):
    path = resolve_store_path(
        config_dir=tmp_path, store_config=StoreConfig(retain_days=10)
    )
    assert path == tmp_path / "dbfresh.db"


def test_find_checks_matches_by_object(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    store.record_observation(
        run_id, _result(object="dbo.fct_sales", metric="row_count", check_id="a")
    )
    store.record_observation(
        run_id, _result(object="dbo.other", metric="row_count", check_id="b")
    )
    candidates = store.find_checks("dbo.fct_sales")
    assert [c["check_id"] for c in candidates] == ["a"]
    store.close()


def test_find_checks_returns_multiple_candidates_for_ambiguous_object(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    store.record_observation(
        run_id, _result(object="dbo.fct_sales", metric="row_count", check_id="a")
    )
    store.record_observation(
        run_id,
        _result(
            object="dbo.fct_sales", metric="null_rate", check_id="b", source="other"
        ),
    )
    candidates = store.find_checks("dbo.fct_sales")
    assert {c["check_id"] for c in candidates} == {"a", "b"}
    store.close()


def test_find_checks_filters_by_source_and_metric(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    store.record_observation(
        run_id,
        _result(object="t", metric="row_count", check_id="a", source="warehouse"),
    )
    store.record_observation(
        run_id,
        _result(object="t", metric="null_rate", check_id="b", source="other"),
    )
    assert [c["check_id"] for c in store.find_checks("t", source="warehouse")] == ["a"]
    assert [c["check_id"] for c in store.find_checks("t", metric="null_rate")] == ["b"]
    store.close()


def test_find_checks_returns_empty_for_unknown_object(tmp_path):
    store = Store(tmp_path / "obs.db")
    store.start_run()
    assert store.find_checks("nonexistent") == []
    store.close()


def test_latest_observation_returns_most_recent_row(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    store.record_observation(
        run_id,
        _result(check_id="x", value="fp-old"),
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    store.record_observation(
        run_id,
        _result(check_id="x", value="fp-new"),
        observed_at=datetime(2026, 7, 5, tzinfo=UTC),
    )
    obs = store.latest_observation("x")
    assert obs["value_text"] == "fp-new"
    store.close()


def test_latest_observation_returns_none_when_no_history(tmp_path):
    store = Store(tmp_path / "obs.db")
    assert store.latest_observation("nonexistent") is None
    store.close()


def test_latest_observation_ignores_other_check_ids(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    store.record_observation(run_id, _result(check_id="a", value=1))
    store.record_observation(run_id, _result(check_id="b", value=2))
    obs = store.latest_observation("a")
    assert obs["value"] == 1.0
    store.close()


def test_latest_fingerprint_observation_skips_a_value_less_skip_or_error(tmp_path):
    # A SKIPPED (skip_off_schedule) or ERROR (unreachable source) schema
    # observation persists with no fingerprint (value_text NULL). The
    # unchanged baseline must never read one of those as "the latest
    # observation" -- that would mask real drift recorded just before it.
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    store.record_observation(
        run_id,
        _result(check_id="x", value="fp-1", status=Status.OK),
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    store.record_observation(
        run_id,
        _result(check_id="x", value=None, status=Status.SKIPPED),
        observed_at=datetime(2026, 7, 2, tzinfo=UTC),
    )
    store.record_observation(
        run_id,
        _result(check_id="x", value=None, status=Status.ERROR, error="boom"),
        observed_at=datetime(2026, 7, 3, tzinfo=UTC),
    )
    obs = store.latest_fingerprint_observation("x")
    assert obs["value_text"] == "fp-1"
    store.close()


def test_latest_fingerprint_observation_picks_the_most_recent_fingerprint(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    store.record_observation(
        run_id,
        _result(check_id="x", value="fp-old", status=Status.OK),
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    store.record_observation(
        run_id,
        _result(check_id="x", value="fp-new", status=Status.FAIL),
        observed_at=datetime(2026, 7, 2, tzinfo=UTC),
    )
    obs = store.latest_fingerprint_observation("x")
    assert obs["value_text"] == "fp-new"
    store.close()


def test_latest_fingerprint_observation_returns_none_with_no_history(tmp_path):
    store = Store(tmp_path / "obs.db")
    assert store.latest_fingerprint_observation("nonexistent") is None
    store.close()


def test_latest_fingerprint_observation_returns_none_when_only_value_less_rows(
    tmp_path,
):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    store.record_observation(
        run_id, _result(check_id="x", value=None, status=Status.SKIPPED)
    )
    store.record_observation(
        run_id, _result(check_id="x", value=None, status=Status.ERROR)
    )
    assert store.latest_fingerprint_observation("x") is None
    store.close()


def test_record_observation_round_trips_freshness_lag_seconds(tmp_path):
    adapter = SqliteAdapter()
    adapter.rows("CREATE TABLE t (created_at TEXT)")
    adapter.rows("INSERT INTO t (created_at) VALUES ('2026-07-10 10:00:00')")
    check = Check(
        source="s",
        object="t",
        metric="freshness",
        column="created_at",
        expect=parse_expectation({"max_lag": "26h"}),
    )
    now = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)  # 10h after created_at
    result = evaluate_check(check, adapter, now=now)
    adapter.close()

    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    store.record_observation(run_id, result, observed_at=now)
    row = store._conn.execute("SELECT value, value_text FROM observation").fetchone()
    assert row["value"] == 36000.0  # 10h lag, in seconds
    assert row["value_text"] is None
    store.close()


def test_observation_table_is_strict_rejects_mistyped_value(tmp_path):
    store = Store(tmp_path / "obs.db")
    store.start_run()
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "INSERT INTO observation (run_id, check_id, source, object, "
            "label, value, status, observed_at, weekday) "
            "VALUES (1, 'c', 's', 'o', 'l', 'not-a-number', 'OK', "
            "'2026-01-01T00:00:00Z', 3)"
        )
    store.close()


def _legacy_store(db: Path) -> None:
    """Write a store file with the observation schema as it stood before the
    expected/error columns existed, plus one row, so a reopen exercises the
    migration path against a real pre-existing file."""
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE run (
          run_id INTEGER PRIMARY KEY, started_at TEXT NOT NULL,
          finished_at TEXT, status TEXT NOT NULL, git_sha TEXT
        ) STRICT;
        CREATE TABLE observation (
          run_id INTEGER NOT NULL REFERENCES run(run_id),
          check_id TEXT NOT NULL, source TEXT NOT NULL, object TEXT NOT NULL,
          metric TEXT, label TEXT NOT NULL, value REAL, value_text TEXT,
          status TEXT NOT NULL, observed_at TEXT NOT NULL, weekday INTEGER NOT NULL
        ) STRICT;
        """
    )
    conn.execute(
        "INSERT INTO run (started_at, status) "
        "VALUES ('2026-01-01T00:00:00+00:00', 'OK')"
    )
    conn.execute(
        "INSERT INTO observation (run_id, check_id, source, object, metric, "
        "label, value, value_text, status, observed_at, weekday) "
        "VALUES (1, 'cid', 'src', 'obj', 'row_count', 'row_count', 5, NULL, "
        "'OK', '2026-01-01T00:00:00+00:00', 3)"
    )
    conn.commit()
    conn.close()


def test_migrate_adds_expected_and_error_to_a_legacy_store(tmp_path):
    db = tmp_path / "obs.db"
    _legacy_store(db)

    store = Store(db)  # opening triggers _migrate
    columns = {
        row["name"] for row in store._conn.execute("PRAGMA table_info(observation)")
    }
    assert {"expected", "error"} <= columns
    # The pre-existing row survives, reading back NULL for the new columns.
    row = store._conn.execute(
        "SELECT * FROM observation WHERE check_id = 'cid'"
    ).fetchone()
    assert row["value"] == 5
    assert row["expected"] is None
    assert row["error"] is None
    store.close()


def test_migrate_is_idempotent_on_a_current_store(tmp_path):
    db = tmp_path / "obs.db"
    Store(db).close()  # first open creates the current schema
    store = Store(db)  # second open: columns already present, no error
    columns = {
        row["name"] for row in store._conn.execute("PRAGMA table_info(observation)")
    }
    assert {"expected", "error"} <= columns
    store.close()


def test_record_observation_persists_expected_and_error(tmp_path):
    store = Store(tmp_path / "obs.db")
    run_id = store.start_run()
    store.record_observation(
        run_id,
        _result(
            check_id="cid_fail",
            status=Status.FAIL,
            value=250,
            expected="between 1 and 100",
        ),
    )
    store.record_observation(
        run_id,
        _result(
            check_id="cid_error",
            status=Status.ERROR,
            value=None,
            error="connection refused",
        ),
    )
    fail = store.latest_observation("cid_fail")
    err = store.latest_observation("cid_error")
    assert fail["expected"] == "between 1 and 100"
    assert fail["error"] is None
    assert err["expected"] is None
    assert err["error"] == "connection refused"
    store.close()


def test_latest_run_returns_most_recent_completed_run(tmp_path):
    store = Store(tmp_path / "obs.db")
    assert store.latest_run() is None  # nothing has finished yet
    first = store.start_run(started_at=datetime(2026, 1, 1, tzinfo=UTC))
    store.finish_run(
        first, Status.OK, finished_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC)
    )
    second = store.start_run(started_at=datetime(2026, 1, 2, tzinfo=UTC))
    store.finish_run(
        second, Status.FAIL, finished_at=datetime(2026, 1, 2, 0, 1, tzinfo=UTC)
    )
    latest = store.latest_run()
    assert latest["run_id"] == second
    assert latest["status"] == "FAIL"
    assert latest["finished_at"] is not None
    store.close()


def test_latest_run_skips_an_in_progress_run(tmp_path):
    store = Store(tmp_path / "obs.db")
    done = store.start_run(started_at=datetime(2026, 1, 1, tzinfo=UTC))
    store.finish_run(
        done, Status.OK, finished_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC)
    )
    store.start_run(started_at=datetime(2026, 1, 2, tzinfo=UTC))  # started, unfinished
    latest = store.latest_run()
    assert latest is not None
    assert latest["run_id"] == done
    assert latest["status"] == "OK"
    store.close()


def test_migrate_tolerates_a_column_added_by_a_concurrent_opener(tmp_path, monkeypatch):
    """A duplicate-column error from a racing opener is swallowed, not raised.

    Forced deterministically: the store already has the new columns, but the
    first ``table_info`` read is made to report an unrelated table's columns,
    so ``_migrate`` believes expected/error are missing and attempts ALTERs
    that conflict. The guard re-reads the real schema and continues.
    """
    db = tmp_path / "obs.db"
    Store(db).close()  # current schema: expected + error already present

    store = Store(db)
    real_columns = store._observation_columns
    calls = {"n": 0}

    def stale_first():
        calls["n"] += 1
        # The first read (building `existing`) lies -- reports neither new
        # column -- so _migrate attempts ALTERs that conflict with the
        # columns already on disk; later reads (the guard's re-check) tell
        # the truth.
        if calls["n"] == 1:
            return {"run_id", "check_id"}
        return real_columns()

    monkeypatch.setattr(store, "_observation_columns", stale_first)
    store._migrate()  # must not raise despite the conflicting ALTERs
    store.close()
