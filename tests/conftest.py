"""Shared pytest configuration.

pytest-textual-snapshot 1.0.0 sets ``SVGImageExtension._file_extension``
(an older syrupy attribute name); the installed syrupy release reads
``file_extension`` instead, so without this patch baseline snapshots would
be written with a generic ``.raw`` extension rather than ``.svg``.

No version guard: this assignment is safe unpinned. If a future
pytest-textual-snapshot release sets ``file_extension`` correctly itself,
this just re-assigns the same value -- a no-op. If a future syrupy release
renames the attribute again, this patch quietly stops helping (no crash);
the only symptom is snapshot files reverting to some new default
extension, which the committed ``tests/__snapshots__/`` filenames and CI
would surface immediately, not silently.
"""

import pytest
from pytest_textual_snapshot import SVGImageExtension

from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.models import Status
from dbfresh.store import Store

SVGImageExtension.file_extension = "svg"


@pytest.fixture
def seed_row_count_db():
    """Create ``t(id INTEGER)`` with three rows (1, 2, 3) at a given path.

    Shared by tests that only need a trivial row-count-able table --
    previously copy-pasted identically across test modules.
    """

    def _seed(path):
        adapter = SqliteAdapter(str(path))
        adapter.rows("CREATE TABLE t (id INTEGER)")
        adapter.rows("INSERT INTO t (id) VALUES (1), (2), (3)")
        adapter.close()

    return _seed


@pytest.fixture
def row_count_config():
    """Write a one-source, one-``row_count``-check config to a given path."""

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

    return _config


@pytest.fixture
def pump_until():
    """Advance the app message loop until predicate() holds (bounded).

    workers.wait_for_complete() waits for a worker thread; the
    StateChanged it posts is handled a loop cycle later, and a loaded CI
    runner can lag past one pause(). Poll for the observable effect
    instead of assuming a fixed number of pauses. The caller still
    asserts afterward, so a genuine failure (the effect never happens)
    surfaces as that assertion rather than a silent pass.
    """

    async def _pump(pilot, predicate, *, tries=100):
        for _ in range(tries):
            if predicate():
                return
            await pilot.pause()

    return _pump


@pytest.fixture
def seed_observations():
    """Persist a sequence of ``(Result, observed_at)`` pairs as one run."""

    def _seed(store_path, entries):
        store = Store(store_path)
        run_id = store.start_run()
        for result, observed_at in entries:
            store.record_observation(run_id, result, observed_at=observed_at)
        store.finish_run(run_id, Status.OK)
        store.close()

    return _seed
