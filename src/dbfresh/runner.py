"""Shared run+persist logic used by the CLI ``run`` command and the TUI.

Both front-ends need the same sequence: build one adapter per source,
evaluate every check, and persist the results to the store. Factored here
once so `dbfresh run` and `dbfresh ui` never duplicate it.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from datetime import UTC, datetime

from dbfresh.adapters.base import Adapter
from dbfresh.checks import Check
from dbfresh.config import Config
from dbfresh.engine import run_checks
from dbfresh.models import Result, RunResult
from dbfresh.store import Store, capture_git_sha


def filter_checks(checks: list[Check], only: str | None) -> list[Check]:
    """Every check, or only those on source ``only`` when given."""
    if only is None:
        return checks
    return [check for check in checks if check.source == only]


def run_and_persist(
    config: Config,
    store: Store | None,
    now: datetime | None = None,
    only: str | None = None,
    on_result: Callable[[Result], None] | None = None,
) -> RunResult:
    """Run every check in ``config`` and persist its results to ``store``.

    ``now`` is resolved once, up front, and reused throughout: it is the
    run's ``started_at`` (the run row opens before any check executes) and
    every observation's ``observed_at``, rather than the wall-clock time at
    which each is written after evaluation finishes. The returned
    ``RunResult`` carries that same ``started_at``, the actual completion
    time as ``finished_at``, and ``run_id`` (``None`` when ``store`` is
    ``None``) for the JSON report's envelope.

    ``only``, when given, restricts the run to a single source's checks --
    every other source is filtered out before adapters are built, so an
    unrelated source is never even connected to, not merely excluded from
    the results.

    Builds one adapter per source actually referenced by the (possibly
    ``only``-filtered) checks (never every configured source -- an
    unrelated, unreachable source must not affect a run that never touches
    it), evaluates every check via :func:`~dbfresh.engine.run_checks`, and
    always closes the adapters afterward. When ``store`` is given, records
    one observation per result inside the run opened up front; ``store``
    itself is left open -- the caller (CLI or TUI) owns its lifecycle, so it
    can be reused across repeated calls (e.g. the TUI's Run action).

    A source whose adapter fails to build (unreachable host, bad
    credentials, unknown type, ...) does not abort the run: its exception is
    recorded in ``failed_sources`` and passed to ``run_checks``, which turns
    every check on that source into an ``ERROR`` result while every other
    source evaluates normally.

    ``on_result``, when given, is forwarded to :func:`~dbfresh.engine.run_checks`
    and called once per check as it completes -- e.g. to advance a progress bar.
    """
    from dbfresh.adapters.factory import create_adapter

    now = now or datetime.now(UTC)
    checks = filter_checks(config.checks, only)

    referenced = {check.source for check in checks}
    adapters: dict[str, Adapter] = {}
    failed_sources: dict[str, BaseException] = {}
    for name in referenced:
        source = config.sources[name]
        try:
            adapters[name] = create_adapter(
                source.type, source.params, timeout=source.timeout
            )
        except Exception as exc:  # connect-time failure -> ERROR per source
            failed_sources[name] = exc

    # Open the run row before evaluation, stamped with the same `now` that
    # evaluation uses, so started_at reflects when the run began rather
    # than when it finished.
    run_id = None
    if store is not None:
        run_id = store.start_run(
            git_sha=capture_git_sha(config.config_dir), started_at=now
        )

    try:
        run = run_checks(
            adapters,
            checks,
            calendar=config.calendar,
            store=store,
            now=now,
            failed_sources=failed_sources,
            on_result=on_result,
        )
    finally:
        for adapter in adapters.values():
            # close every adapter regardless of whether another one raised
            with contextlib.suppress(Exception):
                adapter.close()

    finished_at = datetime.now(UTC)
    if store is not None and run_id is not None:
        store.record_observations(
            run_id, run.results, observed_at=now, calendar=config.calendar
        )
        store.finish_run(run_id, run.status, finished_at=finished_at)

    run.run_id = run_id
    run.started_at = now
    run.finished_at = finished_at
    return run
