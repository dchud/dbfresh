"""SQLite observation store: run/observation history (spec section 8)."""

from __future__ import annotations

import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from dbfresh.engine import Result, Status

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


def _to_utc(value: datetime | None) -> datetime:
    """Default to now; assume naive datetimes are already UTC."""
    value = value or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _split_value(value: Any) -> tuple[float | None, str | None]:
    """Numeric scalars go in ``value``; everything else in ``value_text``."""
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, str(value)
    if isinstance(value, (int, float)):
        return float(value), None
    return None, str(value)


class Store:
    """A local SQLite observation store, separate from the source adapters."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        if str(self._path) != ":memory:":
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
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
        return cur.lastrowid

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

    def record_observation(
        self, run_id: int, result: Result, observed_at: datetime | None = None
    ) -> None:
        """Persist one observation for a check's result, OK or not."""
        observed_at = _to_utc(observed_at)
        value, value_text = _split_value(result.value)
        label = result.label or result.metric or "assert"
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
                observed_at.weekday(),
            ),
        )
        self._conn.commit()

    def history(self, check_id: str, limit: int = 30) -> list[dict]:
        """A check's most recent observations, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM observation WHERE check_id = ? "
            "ORDER BY observed_at DESC LIMIT ?",
            (check_id, limit),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

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
