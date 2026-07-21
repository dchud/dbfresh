"""Leaf module: run/result/status types shared by the engine and the store.

Holds no dependency on ``dbfresh.engine``, ``dbfresh.store``, or
``dbfresh.adapters`` -- both ``engine`` (evaluation) and ``store``
(persistence) import from here instead of from each other, which is what
keeps them from forming an import cycle.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Protocol


class Status(StrEnum):
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"


@dataclass
class Result:
    object: str
    metric: str | None
    status: Status
    source: str = ""
    value: Any = None
    expected: str | None = None
    error: str | None = None
    label: str | None = None
    samples: list[dict[str, Any]] | None = None
    check_id: str | None = None
    diff: list[str] | None = None
    tier: str = "table"


def split_value(value: Any) -> tuple[float | None, str | None]:
    """Numeric scalars go in ``value``; everything else in ``value_text``.

    Shared by the store (persisted columns) and the JSON report (the
    ``value`` / ``value_text`` pair in the stable contract) so a schema
    fingerprint always lands in ``value_text`` and a numeric observation
    always lands in ``value``, in both places, the same way.
    """
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, str(value)
    if isinstance(value, (int, float)):
        return float(value), None
    return None, str(value)


_SEVERITY = {
    Status.OK: 0,
    Status.SKIPPED: 0,
    Status.WARN: 1,
    Status.FAIL: 2,
    Status.ERROR: 3,
}
_RANK_STATUS = {0: Status.OK, 1: Status.WARN, 2: Status.FAIL, 3: Status.ERROR}


def worst_status(statuses: Iterable[Status]) -> Status:
    """The most severe status; OK when empty. ERROR (3) outranks FAIL (2)."""
    rank = max((_SEVERITY[s] for s in statuses), default=0)
    return _RANK_STATUS[rank]


def exit_code(status: Status) -> int:
    """Map a status to a process exit code: OK/SKIPPED 0, WARN 1, FAIL 2, ERROR 3."""
    return _SEVERITY[status]


@dataclass
class RunResult:
    results: list[Result]
    status: Status
    run_id: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ObservationReader(Protocol):
    """Read-only access to prior observations, as needed by check evaluation.

    Matches the subset of :class:`~dbfresh.store.Store` the engine actually
    reads from during evaluation -- the schema ``unchanged`` and
    ``vs_previous`` expectation paths -- not the store's full read/write
    surface (history, find_checks, prune, ...).
    """

    def latest_observation(self, check_id: str) -> dict[str, Any] | None: ...

    def latest_fingerprint_observation(
        self, check_id: str
    ) -> dict[str, Any] | None: ...

    def latest_clean_observation(
        self, check_id: str
    ) -> dict[str, Any] | None: ...

    def last_same_weekday_observation(
        self, check_id: str, run_date: date
    ) -> dict[str, Any] | None: ...
