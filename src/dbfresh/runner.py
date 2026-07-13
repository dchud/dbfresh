"""Shared run+persist logic used by the CLI ``run`` command and the TUI.

Both front-ends need the same sequence: build one adapter per source,
evaluate every check, and persist the results to the store. Factored here
once so `dbfresh run` and `dbfresh ui` never duplicate it.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any

from dbfresh.config import Config
from dbfresh.engine import RunResult, run_checks
from dbfresh.store import Store, capture_git_sha


def run_and_persist(
    config: Config,
    store: Store | None,
    now: datetime | None = None,
) -> RunResult:
    """Run every check in ``config`` and persist its results to ``store``.

    Builds one adapter per source actually referenced by ``config.checks``
    (never every configured source -- an unrelated, unreachable source must
    not affect a run that never touches it), evaluates every check via
    :func:`~dbfresh.engine.run_checks`, and always closes the adapters
    afterward. When ``store`` is given, records one observation per result
    inside a new run; ``store`` itself is left open -- the caller (CLI or
    TUI) owns its lifecycle, so it can be reused across repeated calls
    (e.g. the TUI's Run action).

    A source whose adapter fails to build (unreachable host, bad
    credentials, unknown type, ...) does not abort the run: its exception is
    recorded in ``failed_sources`` and passed to ``run_checks``, which turns
    every check on that source into an ``ERROR`` result while every other
    source evaluates normally.
    """
    from dbfresh.adapters.factory import create_adapter

    referenced = {check.source for check in config.checks}
    adapters: dict[str, Any] = {}
    failed_sources: dict[str, BaseException] = {}
    for name in referenced:
        source = config.sources[name]
        try:
            adapters[name] = create_adapter(source.type, source.params)
        except Exception as exc:  # connect-time failure -> ERROR per source
            failed_sources[name] = exc

    try:
        run = run_checks(
            adapters,
            config.checks,
            calendar=config.calendar,
            store=store,
            now=now,
            failed_sources=failed_sources,
        )
    finally:
        for adapter in adapters.values():
            # close every adapter regardless of whether another one raised
            with contextlib.suppress(Exception):
                adapter.close()

    if store is not None:
        run_id = store.start_run(git_sha=capture_git_sha(config.config_dir))
        for result in run.results:
            store.record_observation(run_id, result, calendar=config.calendar)
        store.finish_run(run_id, run.status)

    return run
