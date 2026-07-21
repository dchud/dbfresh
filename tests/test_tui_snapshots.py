"""Snapshot acceptance tests for the dbfresh TUI screens.

Each test renders a Textual app to SVG via the ``snap_compare`` fixture
(pytest-textual-snapshot) and diffs it against a baseline stored alongside
this file under ``__snapshots__/``. Every fixture here is deterministic: a
fixed config, an observation store seeded at fixed timestamps (never
wall-clock time), a fixed terminal size, and -- for the one screen whose
rendering depends on the current time -- a frozen clock.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.checks import Check, check_id
from dbfresh.engine import Result, RunResult, Status, worst_status
from dbfresh.store import Store
from dbfresh.tui.app import DbfreshApp

_TERMINAL_SIZE = (100, 30)

_T1 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
_T2 = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)
_T3 = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)

_ROW_COUNT = Check(source="orders_db", object="orders", metric="row_count")
_SCHEMA = Check(source="orders_db", object="orders", metric="schema")
_NULL_RATE = Check(
    source="orders_db", object="orders", metric="null_rate", column="email"
)
_FRESHNESS = Check(
    source="orders_db",
    object="orders",
    metric="freshness",
    column="modified_at",
)
_DUPLICATE = Check(
    source="orders_db", object="orders", metric="duplicate_count", key="sku"
)
# Deliberately never observed below, so its dashboard leaf renders "unknown".
_SUM = Check(
    source="orders_db", object="orders", metric="sum", column="amount"
)
_WAREHOUSE_ROW_COUNT = Check(
    source="warehouse", object="shipments", metric="row_count"
)

_CONFIG_YAML = """\
sources:
  orders_db: {{ type: sqlite, database: "{orders_db}" }}
  warehouse: {{ type: sqlite, database: "{warehouse_db}" }}
checks:
  - source: orders_db
    object: orders
    metric: row_count
    expect: {{ between: [1, 100000] }}
  - source: orders_db
    object: orders
    metric: schema
    expect: {{ unchanged: true }}
  - source: orders_db
    object: orders
    metric: null_rate
    column: email
    expect: {{ max: 0.05 }}
  - source: orders_db
    object: orders
    metric: freshness
    column: modified_at
    expect: {{ max_lag: 24h }}
  - source: orders_db
    object: orders
    metric: duplicate_count
    key: sku
    expect: {{ max: 0 }}
  - source: orders_db
    object: orders
    metric: sum
    column: amount
    expect: {{ min: 0 }}
  - source: warehouse
    object: shipments
    metric: row_count
    expect: {{ between: [1, 5000] }}
"""


def _seed(
    store: Store, check: Check, status: Status, value, observed_at: datetime
):
    run_id = store.start_run(started_at=observed_at)
    store.record_observation(
        run_id,
        Result(
            object=check.object,
            metric=check.metric,
            status=status,
            source=check.source,
            value=value,
            check_id=check_id(check),
        ),
        observed_at=observed_at,
    )
    store.finish_run(run_id, status, finished_at=observed_at)


def _build_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """A config with table- and column-level checks across two sources,
    and a store pre-seeded with fixed-timestamp observations covering
    every status plus one check left unobserved ("unknown")."""
    orders_db = tmp_path / "orders.db"
    warehouse_db = tmp_path / "warehouse.db"
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        _CONFIG_YAML.format(orders_db=orders_db, warehouse_db=warehouse_db)
    )

    store_path = tmp_path / "observations.db"
    store = Store(store_path)
    _seed(store, _ROW_COUNT, Status.OK, 80, _T1)
    _seed(store, _ROW_COUNT, Status.OK, 120, _T2)
    _seed(store, _ROW_COUNT, Status.OK, 95, _T3)
    _seed(store, _SCHEMA, Status.WARN, None, _T3)
    _seed(store, _NULL_RATE, Status.FAIL, 0.42, _T3)
    _seed(store, _FRESHNESS, Status.ERROR, None, _T3)
    _seed(store, _DUPLICATE, Status.SKIPPED, None, _T3)
    _seed(store, _WAREHOUSE_ROW_COUNT, Status.OK, 340, _T3)
    store.close()

    return cfg_path, store_path


_FROZEN_NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC)


class _FrozenDateTime(datetime):
    """A ``datetime`` whose ``now()`` always returns a fixed instant.

    Monkeypatched over a module's own ``datetime`` name so a snapshot stays
    stable regardless of the real wall-clock date: ``dbfresh.report`` for
    the digest header timestamp, and ``dbfresh.tui.app`` /
    ``dbfresh.tui.screens`` for the status grid's trailing-7-day window --
    without this, which of _T1/_T2/_T3 fall inside that window (and the
    day-column headers themselves) would drift with whatever day the test
    actually runs on. Every other datetime method (``fromisoformat``,
    ``astimezone``, ...) is inherited unchanged.
    """

    @classmethod
    def now(cls, tz=None):
        return _FROZEN_NOW.astimezone(tz) if tz is not None else _FROZEN_NOW


def test_home_dashboard_shows_mixed_statuses(
    snap_compare, tmp_path, monkeypatch
):
    monkeypatch.setattr("dbfresh.tui.app.datetime", _FrozenDateTime)
    # display_timezone() defaults to the local system zone absent a
    # configured calendar (this fixture has none) -- pin it to UTC so the
    # snapshot is deterministic across machines, not just across runs.
    monkeypatch.setattr(
        "dbfresh.report.display_timezone", lambda calendar: UTC
    )
    cfg_path, store_path = _build_fixture(tmp_path)
    app = DbfreshApp(config_path=cfg_path, store_path=str(store_path))

    assert snap_compare(app, terminal_size=_TERMINAL_SIZE)


def test_object_detail_screen_shows_check_grid_and_legend(
    snap_compare, tmp_path, monkeypatch
):
    monkeypatch.setattr("dbfresh.tui.app.datetime", _FrozenDateTime)
    monkeypatch.setattr("dbfresh.tui.screens.datetime", _FrozenDateTime)
    monkeypatch.setattr(
        "dbfresh.report.display_timezone", lambda calendar: UTC
    )
    cfg_path, store_path = _build_fixture(tmp_path)
    app = DbfreshApp(config_path=cfg_path, store_path=str(store_path))

    # Home grid: orders_db.orders is the first row -- enter drills into its
    # checks, the same drill-in test_history_screen_shows_trend goes one
    # hop further from. This one stops here, so the drill-in grid's own
    # "check" column label and status legend are captured directly rather
    # than only implied by a screen two hops downstream.
    assert snap_compare(app, press=("enter",), terminal_size=_TERMINAL_SIZE)


def _crafted_run_result() -> RunResult:
    """A hand-built run result with a mix of statuses, standing in for a
    real run so the report digest is exercised without touching a source
    adapter or the wall clock."""
    results = [
        Result(
            source="orders_db",
            object="orders",
            metric="row_count",
            status=Status.OK,
            value=812,
            expected="between 1 and 100000",
        ),
        Result(
            source="orders_db",
            object="orders",
            metric="null_rate",
            status=Status.FAIL,
            value=0.42,
            expected="max 0.05",
        ),
        Result(
            source="orders_db",
            object="orders",
            metric="freshness",
            status=Status.WARN,
            value=93600,
            expected="max_lag 24h",
        ),
        Result(
            source="warehouse",
            object="shipments",
            metric="row_count",
            status=Status.ERROR,
            error="connection refused",
        ),
        Result(
            source="orders_db",
            object="orders",
            metric="duplicate_count",
            status=Status.SKIPPED,
        ),
    ]
    return RunResult(
        results=results, status=worst_status(r.status for r in results)
    )


def test_report_screen_shows_failures_and_warnings(
    snap_compare, tmp_path, monkeypatch
):
    monkeypatch.setattr("dbfresh.report.datetime", _FrozenDateTime)
    # display_timezone() defaults to the local system zone absent a
    # configured calendar (this fixture has none) -- pin it to UTC so the
    # snapshot is deterministic across machines, not just across runs.
    monkeypatch.setattr(
        "dbfresh.report.display_timezone", lambda calendar: UTC
    )
    cfg_path, store_path = _build_fixture(tmp_path)
    app = DbfreshApp(config_path=cfg_path, store_path=str(store_path))

    def run_before(pilot):
        pilot.app.last_run = _crafted_run_result()

    assert snap_compare(
        app, run_before=run_before, press=("p",), terminal_size=_TERMINAL_SIZE
    )


def test_configure_screen_initial_layout(snap_compare, tmp_path):
    cfg_path, store_path = _build_fixture(tmp_path)
    app = DbfreshApp(config_path=cfg_path, store_path=str(store_path))

    async def run_before(pilot):
        await pilot.press("c")
        await pilot.pause()
        pilot.app.screen.set_focus(
            None
        )  # no blinking input cursor in the baseline

    assert snap_compare(
        app, run_before=run_before, terminal_size=_TERMINAL_SIZE
    )


def test_configure_screen_new_source_form_at_zero_sources(
    snap_compare, tmp_path
):
    """A brand-new project's config has no sources at all -- Configure opens
    straight into the new-source form rather than the propose form (which
    would just be an empty Select with nothing to do)."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("sources: {}\nchecks: []\n")
    store_path = tmp_path / "observations.db"
    app = DbfreshApp(config_path=cfg_path, store_path=str(store_path))

    async def run_before(pilot):
        await pilot.press("c")
        await pilot.pause()
        pilot.app.screen.set_focus(
            None
        )  # no blinking input cursor in the baseline

    assert snap_compare(
        app, run_before=run_before, terminal_size=_TERMINAL_SIZE
    )


def test_configure_screen_post_propose_layout(snap_compare, tmp_path):
    """Configure's densest state: after a successful Propose, the existing
    (one editable single-scalar check with its value Input + Save button,
    one read-only between check), proposed, and offered panels are all
    populated at once. Guards the three-panel VerticalScroll layout, and
    in particular the editable existing-check row against clipping its
    Input/Save off the right edge.
    """
    db = tmp_path / "data.db"
    adapter = SqliteAdapter(str(db))
    adapter.rows(
        "CREATE TABLE fct (id INTEGER PRIMARY KEY, amount REAL, modified_at TIMESTAMP)"
    )
    adapter.close()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
        "checks:\n"
        "  - source: s\n    object: fct\n    metric: row_count\n"
        "    expect:\n      max: 100\n"
        "  - source: s\n    object: fct\n    metric: row_count\n"
        "    expect:\n      between: [1, 1000]\n    id: fct_between\n"
    )
    app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))

    async def run_before(pilot):
        await pilot.press("c")
        await pilot.pause()
        pilot.app.screen.query_one("#source-select").value = "s"
        pilot.app.screen.query_one("#object-input").value = "fct"
        await pilot.click("#propose-btn")
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        pilot.app.screen.set_focus(
            None
        )  # no blinking input cursor in the baseline

    # Taller than the standard height: the three stacked panels don't fit a
    # 30-row viewport (a 30-row capture shows only the propose form, which
    # the initial-layout snapshot already covers), so this uses a viewport
    # tall enough to actually capture the dense existing/proposed/offered
    # content -- including the editable existing-check row with its Save
    # button, the row the clipping fix keeps on screen.
    assert snap_compare(app, run_before=run_before, terminal_size=(100, 55))
