"""SQLite observation store: run/observation history (spec section 8)."""

from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from dbfresh.calendar import BusinessCalendar
from dbfresh.config import StoreConfig
from dbfresh.models import Result, Status, split_value

_CLEAN_STATUSES = (Status.OK.value, Status.WARN.value, Status.FAIL.value)

_DEFAULT_STORE_FILENAME = "dbfresh.db"

# How long a writer waits on a locked database before raising
# "database is locked" -- long enough that two overlapping `dbfresh run`
# processes (e.g. a cron overlap) serialize instead of failing outright.
_BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS run (
  run_id     INTEGER PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status     TEXT NOT NULL,
  git_sha    TEXT
);

CREATE TABLE IF NOT EXISTS observation (
  run_id    INTEGER NOT NULL REFERENCES run(run_id),
  check_id  TEXT    NOT NULL,
  source    TEXT    NOT NULL,
  object    TEXT    NOT NULL,
  metric    TEXT,
  label     TEXT    NOT NULL,
  value     REAL,
  value_text TEXT,
  status    TEXT    NOT NULL,
  observed_at TEXT  NOT NULL,
  weekday   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_obs_checkid_time
  ON observation(check_id, observed_at);
"""

# Sentinel run status between start_run and finish_run; never a Status value.
_RUN_STARTED = "RUNNING"


def capture_git_sha(path: str | Path) -> str | None:
    """Best-effort ``git rev-parse HEAD`` for the repo containing ``path``.

    Returns ``None`` when ``path`` is not inside a git repository or git is
    unavailable — never raises.
    """
    directory = Path(path)
    if directory.is_file():
        directory = directory.parent
    try:
        proc = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


def resolve_store_path(
    config_dir: Path,
    store_config: StoreConfig | None,
    cli_store: str | None = None,
    env_store: str | None = None,
) -> Path:
    """Store-path precedence: ``--store`` > ``DBFRESH_STORE`` > ``store.path``
    in config > default ``./dbfresh.db`` (spec 8.1).

    A path from the CLI flag or environment variable resolves against CWD,
    like any other command-line path (spec 12.3). A relative ``store.path``
    from config, and the hardcoded default, resolve against the root
    config's directory instead — so a clone of the config repo gets its own
    store file without a machine-specific path being committed.
    """
    if cli_store:
        return Path(cli_store)
    if env_store:
        return Path(env_store)
    if store_config and store_config.path:
        candidate = Path(store_config.path)
        return candidate if candidate.is_absolute() else config_dir / candidate
    return config_dir / _DEFAULT_STORE_FILENAME


def _to_utc(value: datetime | None) -> datetime:
    """Default to now; assume naive datetimes are already UTC."""
    value = value or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class Store:
    """A local SQLite observation store, separate from the source adapters."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        if str(self._path) != ":memory:":
            self._path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: run_checks reads this store (for
        # history-based expectations) from each source's worker thread
        # while evaluation is in progress. Writes -- start_run,
        # record_observation(s), finish_run -- happen only afterward, from
        # the single controller thread that called run_and_persist; worker
        # threads never write. That read-only-during-evaluation invariant
        # is what makes sharing one sqlite3 connection across threads safe
        # here without additional locking.
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL + busy_timeout let two overlapping `dbfresh run` processes
        # read and write this file concurrently instead of hitting
        # "database is locked" under the default rollback-journal mode.
        # journal_mode=WAL is a no-op on an in-memory database (sqlite
        # reports "memory" instead); busy_timeout still applies there.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def start_run(
        self, git_sha: str | None = None, started_at: datetime | None = None
    ) -> int:
        """Record a new run's start; returns its ``run_id``."""
        started_at = _to_utc(started_at)
        cur = self._conn.execute(
            "INSERT INTO run (started_at, status, git_sha) VALUES (?, ?, ?)",
            (started_at.isoformat(), _RUN_STARTED, git_sha),
        )
        self._conn.commit()
        run_id = cur.lastrowid
        if run_id is None:
            # sqlite3 only returns None for lastrowid when the statement
            # wasn't an INSERT (or the table has WITHOUT ROWID) -- neither
            # applies to this fixed INSERT, so this never actually happens.
            raise RuntimeError("run insert did not return a row id")
        return run_id

    def finish_run(
        self, run_id: int, status: Status, finished_at: datetime | None = None
    ) -> None:
        """Record a run's completion: finished_at and its worst status."""
        finished_at = _to_utc(finished_at)
        self._conn.execute(
            "UPDATE run SET finished_at = ?, status = ? WHERE run_id = ?",
            (finished_at.isoformat(), Status(status).value, run_id),
        )
        self._conn.commit()

    def _insert_observation(
        self,
        run_id: int,
        result: Result,
        observed_at: datetime | None,
        calendar: BusinessCalendar | None,
    ) -> None:
        """``INSERT`` one observation row without committing.

        Shared by :meth:`record_observation` (single row, own commit) and
        :meth:`record_observations` (many rows, one commit for all of them).
        ``weekday`` is stored in ``calendar``'s timezone when given, else
        UTC, so ``last_same_weekday_observation`` compares like for like.
        """
        observed_at = _to_utc(observed_at)
        value, value_text = split_value(result.value)
        label = result.label or result.metric or "assert"
        weekday = (
            calendar.local_date(observed_at).weekday()
            if calendar is not None
            else observed_at.weekday()
        )
        self._conn.execute(
            "INSERT INTO observation (run_id, check_id, source, object, metric, "
            "label, value, value_text, status, observed_at, weekday) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                result.check_id,
                result.source,
                result.object,
                result.metric,
                label,
                value,
                value_text,
                Status(result.status).value,
                observed_at.isoformat(),
                weekday,
            ),
        )

    def record_observation(
        self,
        run_id: int,
        result: Result,
        observed_at: datetime | None = None,
        calendar: BusinessCalendar | None = None,
    ) -> None:
        """Persist one observation for a check's result, OK or not."""
        self._insert_observation(run_id, result, observed_at, calendar)
        self._conn.commit()

    def record_observations(
        self,
        run_id: int,
        results: Iterable[Result],
        observed_at: datetime | None = None,
        calendar: BusinessCalendar | None = None,
    ) -> None:
        """Persist every result from one run's evaluation in one transaction.

        Same per-row insert as :meth:`record_observation`, but commits once
        after every row instead of once per row -- a run with many checks
        doesn't turn into that many separate disk syncs, and the run's
        observations either all land or none do.
        """
        for result in results:
            self._insert_observation(run_id, result, observed_at, calendar)
        self._conn.commit()

    def latest_observation(self, check_id: str) -> dict | None:
        """The most recent prior observation for ``check_id``, or ``None``.

        Used by history-based expectations (schema ``unchanged``, and
        ``vs_previous``) to read the prior ``value`` / ``value_text`` /
        ``status`` during evaluation, before the current run persists.
        """
        row = self._conn.execute(
            "SELECT * FROM observation WHERE check_id = ? "
            "ORDER BY observed_at DESC LIMIT 1",
            (check_id,),
        ).fetchone()
        return dict(row) if row else None

    def latest_clean_observation(self, check_id: str) -> dict | None:
        """The most recent prior observation excluding ERROR/SKIPPED.

        Used by ``vs_previous: {baseline: previous}`` so a broken or
        skipped run's null value never becomes the comparison baseline.
        """
        placeholders = ", ".join("?" * len(_CLEAN_STATUSES))
        row = self._conn.execute(
            f"SELECT * FROM observation WHERE check_id = ? "
            f"AND status IN ({placeholders}) "
            "ORDER BY observed_at DESC LIMIT 1",
            (check_id, *_CLEAN_STATUSES),
        ).fetchone()
        return dict(row) if row else None

    def last_same_weekday_observation(
        self, check_id: str, run_date: date
    ) -> dict | None:
        """The most recent prior same-weekday observation, 6+ days back.

        Used by ``vs_previous: {baseline: last_same_weekday}``: matches the
        stored ``weekday`` (already in the calendar timezone) against
        ``run_date``'s weekday, and requires ``observed_at`` to be at least
        6 calendar days before ``run_date`` so a same-week rerun is never
        selected. Excludes ERROR/SKIPPED like :meth:`latest_clean_observation`.
        """
        floor = run_date - timedelta(days=6)
        placeholders = ", ".join("?" * len(_CLEAN_STATUSES))
        rows = self._conn.execute(
            f"SELECT * FROM observation WHERE check_id = ? AND weekday = ? "
            f"AND status IN ({placeholders}) "
            "ORDER BY observed_at DESC",
            (check_id, run_date.weekday(), *_CLEAN_STATUSES),
        ).fetchall()
        for row in rows:
            if datetime.fromisoformat(row["observed_at"]).date() <= floor:
                return dict(row)
        return None

    def history(self, check_id: str, limit: int = 30) -> list[dict]:
        """A check's most recent observations, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM observation WHERE check_id = ? "
            "ORDER BY observed_at DESC LIMIT ?",
            (check_id, limit),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def find_checks(
        self,
        object_: str,
        source: str | None = None,
        metric: str | None = None,
    ) -> list[dict]:
        """Distinct checks observed for ``object_``, optionally narrowed.

        Used to disambiguate ``dbfresh history OBJECT`` when more than one
        check_id has been observed against the same object.
        """
        query = (
            "SELECT DISTINCT check_id, source, object, metric, label "
            "FROM observation WHERE object = ?"
        )
        params: list[Any] = [object_]
        if source is not None:
            query += " AND source = ?"
            params.append(source)
        if metric is not None:
            query += " AND metric = ?"
            params.append(metric)
        query += " ORDER BY source, object, metric, label"
        rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def prune(self, retain_days: int, now: datetime | None = None) -> int:
        """Delete observations (and now-orphaned runs) older than retention."""
        now = _to_utc(now)
        cutoff = (now - timedelta(days=retain_days)).isoformat()
        cur = self._conn.execute(
            "DELETE FROM observation WHERE observed_at < ?", (cutoff,)
        )
        deleted = cur.rowcount
        self._conn.execute(
            "DELETE FROM run WHERE started_at < ? AND run_id NOT IN "
            "(SELECT DISTINCT run_id FROM observation)",
            (cutoff,),
        )
        self._conn.commit()
        return deleted

    def close(self) -> None:
        self._conn.close()
