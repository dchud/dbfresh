import asyncio
import threading

import pytest
import yaml
from helpers import ambiguous_sqlite_table, sqlite_table
from textual.css.query import NoMatches
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    TextArea,
)

from dbfresh.adapters import factory
from dbfresh.adapters.base import Category, Column, ObjectInfo
from dbfresh.adapters.databricks import DatabricksDialect
from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.tui.app import DbfreshApp
from dbfresh.tui.configure import ConfigureScreen


def _table_with_offered_temporal(db):
    """Two temporal columns: ``modified_at`` (conventional, so it's the
    unambiguous auto-proposed freshness column) and ``event_time``, which
    stays a legitimately *offered* freshness column -- it's temporal but
    unconventionally named, so ``pick_timestamp_column`` never proposes
    it. Lets a test exercise the offered-freshness threshold Input on a
    column where the offer isn't excluded as already-proposed."""
    adapter = SqliteAdapter(str(db))
    adapter.rows(
        "CREATE TABLE fct (id INTEGER PRIMARY KEY, amount REAL,"
        " modified_at TIMESTAMP, event_time TIMESTAMP)"
    )
    adapter.close()


class _FakeUnreachableAdapter:
    """A source that fails to connect at construction time, the way a
    real network adapter would against an unreachable host."""

    def __init__(self, timeout=None):
        raise ConnectionError("could not connect")


class _FakeViewAdapter:
    """A Databricks-capable view with no timestamp candidate -- proves
    ``is_view`` reaches ``propose_checks`` so no invalid ``describe_history``
    freshness check gets proposed for it."""

    dialect = DatabricksDialect()

    def scalar(self, sql):
        return 1

    def describe(self, obj):
        column = Column(
            name="id", type="INT", nullable=False, category=Category.NUMERIC
        )
        return ObjectInfo(columns=[column], is_view=True)

    def close(self):
        pass


class _FakeKeylessAdapter:
    """A Databricks-dialect adapter with no key metadata at all -- proves the
    Configure screen explains why no ``duplicate_count`` was proposed rather
    than staying silent about it."""

    dialect = DatabricksDialect()

    def scalar(self, sql):
        return 1

    def describe(self, obj):
        column = Column(
            name="id", type="INT", nullable=False, category=Category.NUMERIC
        )
        return ObjectInfo(columns=[column])

    def close(self):
        pass


def _config(path, db):
    path.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\nchecks: []\n'
    )
    return path


def test_configure_preselects_a_lone_source(tmp_path):
    """With exactly one configured source, Configure preselects it in the
    source Select, so a single-source project needs no dropdown interaction
    before Propose."""

    async def scenario():
        db = tmp_path / "data.db"
        sqlite_table(db)
        cfg = _config(tmp_path / "config.yaml", db)  # exactly one source, "s"

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert app.screen.query_one("#source-select").value == "s"

    asyncio.run(scenario())


async def _accept_and_open_yaml(pilot) -> str:
    """Click Accept, wait for :class:`ProposalYamlScreen` to open on top of
    Configure, and return its rendered YAML text -- the shared assertion
    point for every test proving what Accept shows rather than writes."""
    from dbfresh.tui.configure import ProposalYamlScreen

    await pilot.click("#accept-btn")
    await pilot.pause()
    assert isinstance(pilot.app.screen, ProposalYamlScreen)
    return pilot.app.screen.query_one("#proposal-yaml-text", TextArea).text


def test_configure_screen_accept_shows_proposed_checks_as_yaml(
    tmp_path, pump_until
):
    async def scenario():
        db = tmp_path / "data.db"
        sqlite_table(db)
        cfg = _config(tmp_path / "config.yaml", db)
        original_text = cfg.read_text()

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            assert isinstance(app.screen, ConfigureScreen)
            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "fct"

            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot,
                lambda: any(
                    "row_count" in str(cb.label)
                    for cb in app.screen.query(Checkbox)
                ),
            )

            labels = [str(cb.label) for cb in app.screen.query(Checkbox)]
            assert any("row_count" in label for label in labels)
            assert any("schema" in label for label in labels)
            accept_btn = app.screen.query_one("#accept-btn")
            assert not accept_btn.disabled

            yaml_text = await _accept_and_open_yaml(pilot)

        rendered = yaml.safe_load(yaml_text)
        metrics = {c["metric"] for c in rendered["tables"][0]["checks"]}
        assert {"schema", "row_count", "freshness"} <= metrics
        # Accept never writes -- the config file is exactly as it started.
        assert cfg.read_text() == original_text

    asyncio.run(scenario())


def test_configure_screen_passes_is_view_so_no_freshness_is_proposed(
    tmp_path, monkeypatch
):
    async def scenario():
        monkeypatch.setitem(factory._ADAPTERS, "fakeview", _FakeViewAdapter)
        monkeypatch.setitem(factory._DIALECTS, "fakeview", DatabricksDialect)

        cfg = tmp_path / "config.yaml"
        cfg.write_text("sources:\n  s: { type: fakeview }\nchecks: []\n")

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "v"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            labels = [str(cb.label) for cb in app.screen.query(Checkbox)]
            assert not any("freshness" in label for label in labels)

    asyncio.run(scenario())


def test_configure_screen_notes_when_engine_cannot_introspect_keys(
    tmp_path, monkeypatch, pump_until
):
    async def scenario():
        monkeypatch.setitem(factory._ADAPTERS, "keyless", _FakeKeylessAdapter)
        monkeypatch.setitem(factory._DIALECTS, "keyless", DatabricksDialect)

        cfg = tmp_path / "config.yaml"
        cfg.write_text("sources:\n  s: { type: keyless }\nchecks: []\n")

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "t"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot,
                lambda: (
                    "cannot introspect key"
                    in str(app.screen.query_one("#proposal-text").content)
                ),
            )

            proposal_text = str(app.screen.query_one("#proposal-text").content)
            assert "cannot introspect key" in proposal_text

    asyncio.run(scenario())


def test_configure_screen_notes_ambiguous_timestamp_without_a_pick(
    tmp_path, pump_until
):
    async def scenario():
        db = tmp_path / "data.db"
        ambiguous_sqlite_table(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot,
                lambda: (
                    "created_at"
                    in str(app.screen.query_one("#proposal-text").content)
                ),
            )

            proposal_text = str(app.screen.query_one("#proposal-text").content)
            assert "created_at" in proposal_text
            assert "updated_at" in proposal_text
            assert "freshness" not in proposal_text

    asyncio.run(scenario())


def test_configure_screen_uses_picked_timestamp_column(tmp_path, pump_until):
    async def scenario():
        db = tmp_path / "data.db"
        ambiguous_sqlite_table(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            app.screen.query_one("#timestamp-input").value = "updated_at"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot,
                lambda: any(
                    "freshness" in str(cb.label)
                    for cb in app.screen.query(Checkbox)
                ),
            )

            labels = [str(cb.label) for cb in app.screen.query(Checkbox)]
            assert any("freshness" in label for label in labels)

            yaml_text = await _accept_and_open_yaml(pilot)

        rendered = yaml.safe_load(yaml_text)
        freshness = next(
            c
            for c in rendered["tables"][0]["checks"]
            if c["metric"] == "freshness"
        )
        assert freshness["column"] == "updated_at"

    asyncio.run(scenario())


def test_configure_screen_unreachable_source_shows_error_not_crash(
    tmp_path, monkeypatch, pump_until
):
    async def scenario():
        monkeypatch.setitem(
            factory._ADAPTERS, "unreachable", _FakeUnreachableAdapter
        )

        cfg = tmp_path / "config.yaml"
        cfg.write_text("sources:\n  s: { type: unreachable }\nchecks: []\n")

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot,
                lambda: any(
                    "could not connect" in n.message
                    for n in app._notifications
                ),
            )

            # Did not crash: still on the Configure screen, with an error
            # toast rather than a crash -- Propose's connect failure runs
            # through the same notify() channel Accept's own errors use.
            assert isinstance(app.screen, ConfigureScreen)
            messages = [n.message for n in app._notifications]
            assert any("could not connect" in m for m in messages)
            accept_btn = app.screen.query_one("#accept-btn")
            assert accept_btn.disabled

    asyncio.run(scenario())


def test_propose_runs_in_a_worker_thread_with_a_busy_state(
    tmp_path, monkeypatch, pump_until
):
    """Propose's introspection (create_adapter + describe(), via
    check_object_exists) runs off the main thread: while it's in flight,
    the screen stays responsive (queryable, nothing yet mounted from a
    result that hasn't arrived) rather than freezing on a slow/unreachable
    source, and shows a busy state the whole time."""

    async def scenario():
        started = threading.Event()
        release = threading.Event()

        class _BlockingAdapter:
            dialect = DatabricksDialect()

            def scalar(self, sql):
                return 1

            def describe(self, obj):
                started.set()
                assert release.wait(timeout=2), (
                    "test never released describe()"
                )
                column = Column(
                    name="id",
                    type="INT",
                    nullable=False,
                    category=Category.NUMERIC,
                )
                return ObjectInfo(columns=[column])

            def close(self):
                pass

        monkeypatch.setitem(factory._ADAPTERS, "blocking", _BlockingAdapter)
        monkeypatch.setitem(factory._DIALECTS, "blocking", DatabricksDialect)

        cfg = tmp_path / "config.yaml"
        cfg.write_text("sources:\n  s: { type: blocking }\nchecks: []\n")

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "t"
            await pilot.click("#propose-btn")
            await pilot.pause()

            assert started.wait(timeout=2)
            propose_btn = app.screen.query_one("#propose-btn", Button)
            assert propose_btn.disabled
            assert app.screen.sub_title == "proposing checks…"
            assert not app.screen.query(Checkbox)  # nothing mounted yet

            # The event loop kept servicing messages meanwhile -- the
            # screen is still responsive to further queries.
            assert isinstance(app.screen, ConfigureScreen)

            release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot,
                lambda: any(
                    "schema" in str(cb.label)
                    for cb in app.screen.query(Checkbox)
                ),
            )

            assert not propose_btn.disabled
            assert app.screen.sub_title is None
            labels = [str(cb.label) for cb in app.screen.query(Checkbox)]
            assert any("schema" in label for label in labels)

    asyncio.run(scenario())


def test_dismissing_configure_while_propose_is_in_flight_does_not_crash(
    tmp_path, monkeypatch
):
    """Escape-dismissing Configure while its Propose worker is still blocked
    on a slow source must not crash: unmounting the screen cancels the
    worker, and the resulting CANCELLED state has to be handled without
    reaching a torn-down screen to query a removed widget."""

    async def scenario():
        started = threading.Event()
        release = threading.Event()

        class _BlockingAdapter:
            dialect = DatabricksDialect()

            def scalar(self, sql):
                return 1

            def describe(self, obj):
                started.set()
                assert release.wait(timeout=2), (
                    "test never released describe()"
                )
                return ObjectInfo(columns=[])

            def close(self):
                pass

        monkeypatch.setitem(factory._ADAPTERS, "blocking", _BlockingAdapter)
        monkeypatch.setitem(factory._DIALECTS, "blocking", DatabricksDialect)

        cfg = tmp_path / "config.yaml"
        cfg.write_text("sources:\n  s: { type: blocking }\nchecks: []\n")

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "t"
            await pilot.click("#propose-btn")
            await pilot.pause()

            assert started.wait(timeout=2)  # worker now blocked in describe()
            await pilot.press("escape")  # dismiss Configure mid-propose
            await pilot.pause()
            assert not isinstance(app.screen, ConfigureScreen)  # back on Home

            release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()

            # No crash from the CANCELLED state reaching the dismissed
            # screen: still on Home and the grid is still queryable.
            assert not isinstance(app.screen, ConfigureScreen)
            app.query_one("#dashboard-grid")

    asyncio.run(scenario())


def test_configure_screen_unknown_object_disables_accept(tmp_path, pump_until):
    async def scenario():
        db = tmp_path / "data.db"
        sqlite_table(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "does_not_exist"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot,
                lambda: any(
                    "not found" in n.message for n in app._notifications
                ),
            )

            messages = [n.message for n in app._notifications]
            assert any("not found" in m for m in messages)
            accept_btn = app.screen.query_one("#accept-btn")
            assert accept_btn.disabled

    asyncio.run(scenario())


def test_configure_screen_cancel_button_dismisses(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        sqlite_table(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.click("#cancel-btn")
            await pilot.pause()

            assert not isinstance(app.screen, ConfigureScreen)

    asyncio.run(scenario())


def test_configure_screen_escape_cancels(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        sqlite_table(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            assert isinstance(app.screen, ConfigureScreen)
            await pilot.press("escape")
            await pilot.pause()

            assert not isinstance(app.screen, ConfigureScreen)

    asyncio.run(scenario())


def test_configure_screen_trim_deselects_a_proposed_check(
    tmp_path, pump_until
):
    async def scenario():
        db = tmp_path / "data.db"
        sqlite_table(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot,
                lambda: any(
                    "freshness" in str(cb.label)
                    for cb in app.screen.query(Checkbox)
                ),
            )

            freshness_cb = next(
                cb
                for cb in app.screen.query(Checkbox)
                if "freshness" in str(cb.label)
            )
            assert freshness_cb.value is True  # proposed checks start selected
            freshness_cb.value = False

            yaml_text = await _accept_and_open_yaml(pilot)

        rendered = yaml.safe_load(yaml_text)
        metrics = [c["metric"] for c in rendered["tables"][0]["checks"]]
        assert "freshness" not in metrics
        assert "schema" in metrics
        assert "row_count" in metrics

    asyncio.run(scenario())


def test_configure_screen_offered_check_can_be_selected(tmp_path, pump_until):
    async def scenario():
        db = tmp_path / "data.db"
        sqlite_table(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot, lambda: bool(app.screen.query("#offered-amount-sum"))
            )

            # "sum" over the numeric "amount" column is offered but is not
            # part of the base proposed bundle, and starts unselected -- the
            # same default as the CLI wizard's "blank to skip".
            sum_cb = app.screen.query_one("#offered-amount-sum", Checkbox)
            assert sum_cb.value is False
            sum_cb.value = True

            yaml_text = await _accept_and_open_yaml(pilot)

        rendered = yaml.safe_load(yaml_text)
        sum_check = next(
            c for c in rendered["tables"][0]["checks"] if c["metric"] == "sum"
        )
        assert sum_check["column"] == "amount"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "metric,build_table,column,set_value,default_text,expect_key,expect_value",
    [
        pytest.param(
            "null_rate",
            sqlite_table,
            "amount",
            None,
            "0.05",
            "max",
            0.05,
            id="null_rate-default",
        ),
        pytest.param(
            "null_rate",
            sqlite_table,
            "amount",
            "0.2",
            "0.05",
            "max",
            0.2,
            id="null_rate-custom",
        ),
        pytest.param(
            "freshness",
            _table_with_offered_temporal,
            "event_time",
            None,
            "24h",
            "max_lag",
            "24h",
            id="freshness-default",
        ),
        pytest.param(
            "freshness",
            _table_with_offered_temporal,
            "event_time",
            "6h",
            "24h",
            "max_lag",
            "6h",
            id="freshness-custom",
        ),
    ],
)
def test_configure_screen_offered_check_value_is_rendered(
    tmp_path,
    metric,
    build_table,
    column,
    set_value,
    default_text,
    expect_key,
    expect_value,
    pump_until,
):
    """The threshold Input beside an offered null_rate/freshness checkbox is
    pre-filled with the CLI wizard's own default for that metric (asserted
    before touching it, so the default-value coverage from the un-parametrized
    version survives), and Accept renders whichever value sits in the Input
    at that point -- the untouched default, or a value typed over it."""

    async def scenario():
        db = tmp_path / "data.db"
        build_table(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot,
                lambda: bool(
                    app.screen.query(f"#offered-value-{column}-{metric}")
                ),
            )

            value_input = app.screen.query_one(
                f"#offered-value-{column}-{metric}"
            )
            assert value_input.value == default_text
            if set_value is not None:
                value_input.value = set_value

            app.screen.query_one(f"#offered-{column}-{metric}").value = True
            yaml_text = await _accept_and_open_yaml(pilot)

        rendered = yaml.safe_load(yaml_text)
        check = next(
            c
            for c in rendered["tables"][0]["checks"]
            if c["metric"] == metric and c["column"] == column
        )
        assert check["expect"][expect_key] == expect_value

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "metric,build_table,column,invalid_value",
    [
        pytest.param(
            "null_rate",
            sqlite_table,
            "amount",
            "not-a-number",
            id="null_rate-invalid",
        ),
        pytest.param(
            "freshness",
            _table_with_offered_temporal,
            "event_time",
            "not-a-duration",
            id="freshness-invalid",
        ),
    ],
)
def test_configure_screen_offered_check_invalid_value_shows_error_toast(
    tmp_path, metric, build_table, column, invalid_value, pump_until
):
    """An offered null_rate/freshness threshold that doesn't parse for its
    metric is reported back as an error toast -- the same value formats the
    CLI wizard's prompt accepts -- rather than being rendered, or crashing
    the screen."""

    async def scenario():
        db = tmp_path / "data.db"
        build_table(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot,
                lambda: bool(app.screen.query(f"#offered-{column}-{metric}")),
            )

            app.screen.query_one(
                f"#offered-value-{column}-{metric}"
            ).value = invalid_value
            app.screen.query_one(f"#offered-{column}-{metric}").value = True
            await pilot.click("#accept-btn")
            await pilot.pause()

            # Did not crash: still on the Configure screen (no YAML modal
            # opened), with an error toast.
            assert isinstance(app.screen, ConfigureScreen)
            messages = [n.message for n in app._notifications]
            assert any(invalid_value in m for m in messages)

    asyncio.run(scenario())


def test_configure_screen_does_not_offer_metric_already_proposed_for_column(
    tmp_path, pump_until
):
    async def scenario():
        db = tmp_path / "data.db"
        _table_with_offered_temporal(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot,
                lambda: bool(
                    app.screen.query("#offered-event_time-freshness")
                ),
            )

            # ``modified_at`` already has a proposed freshness checkbox --
            # offering it a second time would collide on check_id and get
            # silently dropped by partition_new_checks's dedup, so no
            # offered checkbox exists for it.
            assert not app.screen.query("#offered-modified_at-freshness")
            # ``event_time`` is temporal but wasn't auto-proposed, so it's
            # still legitimately offered.
            assert app.screen.query("#offered-event_time-freshness")

    asyncio.run(scenario())


def test_configure_screen_deselecting_everything_accepts_nothing(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        sqlite_table(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            for cb in app.screen.query(Checkbox):
                cb.value = False

            await pilot.click("#accept-btn")
            await pilot.pause()

            # Nothing was selected, so Accept is a no-op: no YAML modal
            # opened, still on Configure.
            assert isinstance(app.screen, ConfigureScreen)

    asyncio.run(scenario())


def test_configure_screen_accept_warns_when_everything_dedups_away(
    tmp_path, pump_until
):
    """Every check the table would propose already exists in the config
    (same source/object/metric/column identity, regardless of threshold),
    so Accept's dedup (:func:`~dbfresh.configurator.partition_new_checks`)
    excludes all of them from the rendered YAML. Accept must still tell the
    user that, rather than opening an empty modal as if there were
    something new to copy."""

    async def scenario():
        db = tmp_path / "data.db"
        sqlite_table(db)
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
            "checks:\n"
            "  - source: s\n"
            "    object: fct\n"
            "    metric: schema\n"
            "    expect: { unchanged: true }\n"
            "  - source: s\n"
            "    object: fct\n"
            "    metric: row_count\n"
            "    expect: { vs_previous: { baseline: previous, min_ratio: 0.5 } }\n"
            "  - source: s\n"
            "    object: fct\n"
            "    metric: freshness\n"
            "    column: modified_at\n"
            "    expect: { max_lag: 24h }\n"
            "  - source: s\n"
            "    object: fct\n"
            "    metric: duplicate_count\n"
            "    key: id\n"
            "    expect: { max: 0 }\n"
        )

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot, lambda: not app.screen.query_one("#accept-btn").disabled
            )

            # Every proposed checkbox starts checked, and every one of
            # them collides with an existing check -- select everything
            # as-is and accept it.
            await pilot.click("#accept-btn")
            await pilot.pause()

            # No new checks to show, so no modal opens.
            assert isinstance(app.screen, ConfigureScreen)
            messages = [n.message for n in app._notifications]
            assert any("already defined" in m for m in messages)

    asyncio.run(scenario())


def test_configure_screen_propose_and_accept_preserve_manually_tuned_checks(
    tmp_path, pump_until
):
    """The Configure screen must open/propose cleanly against a config that
    already carries manually-tuned checks (non-default thresholds someone
    edited by hand) for the very object being configured, and Accept must
    not render a duplicate that would overwrite the tuned value if pasted.
    A freshly proposed freshness check on the same column collides on
    check_id with the existing hand-tuned one -- identity deliberately
    ignores `expect` -- and is excluded from the rendered YAML by
    :func:`~dbfresh.configurator.partition_new_checks`."""

    async def scenario():
        db = tmp_path / "data.db"
        sqlite_table(db)
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
            "checks:\n"
            "  - source: s\n"
            "    object: fct\n"
            "    metric: freshness\n"
            "    column: modified_at\n"
            "    freshness_source: column\n"
            "    expect:\n"
            "      max_lag: 2h\n"
        )

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot,
                lambda: bool(
                    app.screen.query("#offered-value-amount-null_rate")
                ),
            )

            # The offered null_rate threshold Input still defaults to the
            # CLI wizard's own default -- the wizard never reads existing
            # check values back out of the config to seed a default.
            value_input = app.screen.query_one(
                "#offered-value-amount-null_rate"
            )
            assert value_input.value == "0.05"

            accept_btn = app.screen.query_one("#accept-btn")
            assert not accept_btn.disabled
            yaml_text = await _accept_and_open_yaml(pilot)

            messages = [n.message for n in app._notifications]
            assert any("already defined" in m for m in messages)

        rendered = yaml.safe_load(yaml_text)
        # The proposed freshness (default 24h) collided on identity with
        # the existing hand-tuned one and was excluded -- the modal never
        # offers a block that would clobber the tuned threshold if pasted.
        assert not any(
            c["metric"] == "freshness" for c in rendered["tables"][0]["checks"]
        )

    asyncio.run(scenario())


def _config_with_existing_checks(cfg_path, db):
    cfg_path.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
        "checks:\n"
        "  - source: s\n"
        "    object: fct\n"
        "    metric: row_count\n"
        "    expect:\n"
        "      max: 100\n"
        "  - source: s\n"
        "    object: fct\n"
        "    metric: row_count\n"
        "    expect:\n"
        "      between: [1, 1000]\n"
        "    id: fct_between\n"
    )
    return cfg_path


def test_configure_screen_shows_no_existing_checks_placeholder(
    tmp_path, pump_until
):
    async def scenario():
        db = tmp_path / "data.db"
        sqlite_table(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot,
                lambda: bool(
                    app.screen.query_one("#existing-checks").children
                ),
            )

            existing = app.screen.query_one("#existing-checks")
            assert "(none yet)" in str(existing.children[0].render())

    asyncio.run(scenario())


def test_configure_screen_existing_checks_shown_read_only(
    tmp_path, pump_until
):
    """The object's already-written checks are listed for reference beside
    the proposal -- each as plain text (label plus its expectation, see
    ``dbfresh.tui.dashboard.check_expectation_line``) -- with no Input or
    Save button anywhere in the panel: config is a file the user edits by
    hand, this panel only shows what's already there."""

    async def scenario():
        db = tmp_path / "data.db"
        sqlite_table(db)
        cfg = _config_with_existing_checks(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot,
                lambda: bool(
                    app.screen.query_one("#existing-checks").children
                ),
            )

            existing = app.screen.query_one("#existing-checks")
            lines = [str(child.render()) for child in existing.children]
            assert any("100" in line for line in lines)
            assert any("between" in line for line in lines)
            assert not existing.query(Input)
            assert not existing.query(Button)

    asyncio.run(scenario())


# -- proposed-check threshold input -----------------------------------------
#
# sqlite_table's schema (id PK, amount REAL, modified_at TIMESTAMP) always
# proposes in the same order: schema(0), row_count(1), freshness(2),
# duplicate_count(3) -- so "proposed-value-2" is freshness's Input across
# these tests, matching the fixture's fixed shape.


def test_configure_screen_proposed_freshness_has_a_value_input_prefilled_with_default(
    tmp_path, pump_until
):
    async def scenario():
        db = tmp_path / "data.db"
        sqlite_table(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot,
                lambda: (
                    len(app.screen._proposed) > 2
                    and app.screen._proposed[2]["metric"] == "freshness"
                ),
            )

            assert app.screen._proposed[2]["metric"] == "freshness"
            value_input = app.screen.query_one("#proposed-value-2")
            assert value_input.value == "24h"

    asyncio.run(scenario())


def test_configure_screen_non_freshness_proposed_checks_have_no_value_input(
    tmp_path,
):
    async def scenario():
        db = tmp_path / "data.db"
        sqlite_table(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            for i, block in enumerate(app.screen._proposed):
                if block["metric"] == "freshness":
                    continue
                with pytest.raises(NoMatches):
                    app.screen.query_one(f"#proposed-value-{i}")

    asyncio.run(scenario())


def test_configure_screen_accept_uses_edited_proposed_freshness_value(
    tmp_path, pump_until
):
    async def scenario():
        db = tmp_path / "data.db"
        sqlite_table(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot, lambda: bool(app.screen.query("#proposed-value-2"))
            )

            app.screen.query_one("#proposed-value-2").value = "48h"
            yaml_text = await _accept_and_open_yaml(pilot)

        rendered = yaml.safe_load(yaml_text)
        freshness_checks = [
            c
            for c in rendered["tables"][0]["checks"]
            if c["metric"] == "freshness"
        ]
        assert len(freshness_checks) == 1
        assert freshness_checks[0]["expect"]["max_lag"] == "48h"

    asyncio.run(scenario())


def test_configure_screen_accept_invalid_proposed_freshness_value_shows_error(
    tmp_path, pump_until
):
    async def scenario():
        db = tmp_path / "data.db"
        sqlite_table(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot, lambda: bool(app.screen.query("#proposed-value-2"))
            )

            app.screen.query_one("#proposed-value-2").value = "not-a-duration"
            await pilot.click("#accept-btn")
            await pilot.pause()

            # Errors keep the screen open rather than opening a modal with
            # a partially-accepted bundle.
            assert isinstance(app.screen, ConfigureScreen)
            messages = [n.message for n in app._notifications]
            assert any("invalid max lag" in m for m in messages)

    asyncio.run(scenario())


def test_configure_screen_unchecking_proposed_freshness_ignores_its_value(
    tmp_path, pump_until
):
    async def scenario():
        db = tmp_path / "data.db"
        sqlite_table(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot, lambda: bool(app.screen.query("#proposed-value-2"))
            )

            # An unparseable value in an unchecked freshness row must never
            # block Accept -- only checked rows are rebuilt and validated.
            app.screen.query_one("#proposed-value-2").value = "not-a-duration"
            app.screen.query_one(
                "#proposed-2-freshness", Checkbox
            ).value = False
            yaml_text = await _accept_and_open_yaml(pilot)

        rendered = yaml.safe_load(yaml_text)
        assert not any(
            c["metric"] == "freshness" for c in rendered["tables"][0]["checks"]
        )

    asyncio.run(scenario())


# -- object-input autocomplete (source-edit-and-object-picker) -------------


def _config_with_two_sources_and_checks(cfg_path, db_a, db_b):
    cfg_path.write_text(
        f"sources:\n"
        f'  a: {{ type: sqlite, database: "{db_a}" }}\n'
        f'  b: {{ type: sqlite, database: "{db_b}" }}\n'
        "checks:\n"
        "  - source: a\n"
        "    object: fct_a\n"
        "    metric: row_count\n"
        "    expect:\n"
        "      max: 100\n"
        "  - source: b\n"
        "    object: fct_b\n"
        "    metric: row_count\n"
        "    expect:\n"
        "      max: 100\n"
    )
    return cfg_path


def test_object_input_suggester_built_on_mount_for_preselected_source(
    tmp_path,
):
    async def scenario():
        db = tmp_path / "data.db"
        sqlite_table(db)
        cfg = _config_with_existing_checks(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            assert app.screen.query_one("#source-select").value == "s"
            object_input = app.screen.query_one("#object-input", Input)
            assert object_input.suggester is not None
            suggestion = await object_input.suggester.get_suggestion("f")
            assert suggestion == "fct"

    asyncio.run(scenario())


def test_object_input_suggester_rebuilds_when_source_select_changes(tmp_path):
    async def scenario():
        db_a = tmp_path / "a.db"
        db_b = tmp_path / "b.db"
        sqlite_table(db_a)
        sqlite_table(db_b)
        cfg = _config_with_two_sources_and_checks(
            tmp_path / "config.yaml", db_a, db_b
        )

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "a"
            await pilot.pause()
            object_input = app.screen.query_one("#object-input", Input)
            assert (
                await object_input.suggester.get_suggestion("fct") == "fct_a"
            )

            app.screen.query_one("#source-select").value = "b"
            await pilot.pause()
            assert (
                await object_input.suggester.get_suggestion("fct") == "fct_b"
            )

    asyncio.run(scenario())


def test_object_input_suggester_empty_when_source_has_no_known_objects(
    tmp_path,
):
    async def scenario():
        db = tmp_path / "data.db"
        sqlite_table(db)
        cfg = _config(
            tmp_path / "config.yaml", db
        )  # source "s", no checks yet

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            object_input = app.screen.query_one("#object-input", Input)
            assert (
                await object_input.suggester.get_suggestion("anything") is None
            )

    asyncio.run(scenario())


# -- ProposalYamlScreen (Accept's read-only YAML modal) ---------------------


def test_proposal_yaml_screen_names_the_config_path(tmp_path, pump_until):
    async def scenario():
        db = tmp_path / "data.db"
        sqlite_table(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot, lambda: not app.screen.query_one("#accept-btn").disabled
            )

            await _accept_and_open_yaml(pilot)

            note = str(app.screen.query_one("#proposal-yaml-note").render())
            assert str(cfg) in note
            assert "tables:" in note

    asyncio.run(scenario())


def test_proposal_yaml_screen_copy_button_copies_to_clipboard(
    tmp_path, pump_until
):
    async def scenario():
        db = tmp_path / "data.db"
        sqlite_table(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot, lambda: not app.screen.query_one("#accept-btn").disabled
            )

            yaml_text = await _accept_and_open_yaml(pilot)
            await pilot.click("#proposal-yaml-copy-btn")
            await pilot.pause()

            assert app._clipboard == yaml_text

    asyncio.run(scenario())


def test_proposal_yaml_screen_close_button_returns_to_configure(
    tmp_path, pump_until
):
    async def scenario():
        db = tmp_path / "data.db"
        sqlite_table(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot, lambda: not app.screen.query_one("#accept-btn").disabled
            )

            await _accept_and_open_yaml(pilot)
            await pilot.click("#proposal-yaml-close-btn")
            await pilot.pause()

            # Back on Configure, not Home -- Accept only ever opens a modal
            # on top of it, never dismisses Configure itself.
            assert isinstance(app.screen, ConfigureScreen)

    asyncio.run(scenario())


def test_proposal_yaml_screen_escape_returns_to_configure(
    tmp_path, pump_until
):
    async def scenario():
        db = tmp_path / "data.db"
        sqlite_table(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot, lambda: not app.screen.query_one("#accept-btn").disabled
            )

            await _accept_and_open_yaml(pilot)
            await pilot.press("escape")
            await pilot.pause()

            assert isinstance(app.screen, ConfigureScreen)

    asyncio.run(scenario())


def test_proposal_yaml_screen_text_area_is_read_only(tmp_path, pump_until):
    async def scenario():
        db = tmp_path / "data.db"
        sqlite_table(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot, lambda: not app.screen.query_one("#accept-btn").disabled
            )

            await _accept_and_open_yaml(pilot)
            text_area = app.screen.query_one("#proposal-yaml-text", TextArea)
            assert text_area.read_only

    asyncio.run(scenario())
