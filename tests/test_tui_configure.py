import asyncio
import threading

import pytest
import yaml
from textual.css.query import NoMatches
from textual.widgets import Button, Checkbox, DataTable

from dbfresh.adapters import factory
from dbfresh.adapters.base import Category, Column, ObjectInfo
from dbfresh.adapters.databricks import DatabricksDialect
from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.config import load_config
from dbfresh.tui import app as app_module
from dbfresh.tui.app import DbfreshApp
from dbfresh.tui.configure import ConfigureScreen


def _table(db):
    adapter = SqliteAdapter(str(db))
    adapter.rows(
        "CREATE TABLE fct (id INTEGER PRIMARY KEY, amount REAL, modified_at TIMESTAMP)"
    )
    adapter.close()


def _ambiguous_table(db):
    adapter = SqliteAdapter(str(db))
    adapter.rows(
        "CREATE TABLE fct (id INTEGER PRIMARY KEY, created_at TIMESTAMP,"
        " updated_at TIMESTAMP)"
    )
    adapter.close()


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


def test_configure_screen_proposes_and_appends_checks(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _table(db)
        cfg = _config(tmp_path / "config.yaml", db)

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

            labels = [str(cb.label) for cb in app.screen.query(Checkbox)]
            assert any("row_count" in label for label in labels)
            assert any("schema" in label for label in labels)
            accept_btn = app.screen.query_one("#accept-btn")
            assert not accept_btn.disabled

            await pilot.click("#accept-btn")
            await pilot.pause()

            # Back on Home; the config reloaded with the new checks.
            assert not isinstance(app.screen, ConfigureScreen)

        data = yaml.safe_load(cfg.read_text())
        metrics = {c["metric"] for c in data["checks"]}
        assert {"schema", "row_count", "freshness"} <= metrics

    asyncio.run(scenario())


def test_configure_screen_dashboard_reflects_appended_checks(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _table(db)
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
            await pilot.click("#accept-btn")
            await pilot.pause()

            table = app.query_one("#dashboard-grid", DataTable)
            row_keys = {key.value for key in table.rows}
            assert "s\x1ffct" in row_keys

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
    tmp_path, monkeypatch
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

            proposal_text = str(app.screen.query_one("#proposal-text").content)
            assert "cannot introspect key" in proposal_text

    asyncio.run(scenario())


def test_configure_screen_notes_ambiguous_timestamp_without_a_pick(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _ambiguous_table(db)
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

            proposal_text = str(app.screen.query_one("#proposal-text").content)
            assert "created_at" in proposal_text
            assert "updated_at" in proposal_text
            assert "freshness" not in proposal_text

    asyncio.run(scenario())


def test_configure_screen_uses_picked_timestamp_column(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _ambiguous_table(db)
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

            labels = [str(cb.label) for cb in app.screen.query(Checkbox)]
            assert any("freshness" in label for label in labels)

            await pilot.click("#accept-btn")
            await pilot.pause()

        data = yaml.safe_load(cfg.read_text())
        freshness = next(c for c in data["checks"] if c["metric"] == "freshness")
        assert freshness["column"] == "updated_at"

    asyncio.run(scenario())


def test_configure_screen_unreachable_source_shows_error_not_crash(
    tmp_path, monkeypatch
):
    async def scenario():
        monkeypatch.setitem(factory._ADAPTERS, "unreachable", _FakeUnreachableAdapter)

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

            # Did not crash: still on the Configure screen, with an error
            # toast rather than a crash -- Propose's connect failure runs
            # through the same notify() channel as every other error on
            # this screen (Save, Accept).
            assert isinstance(app.screen, ConfigureScreen)
            messages = [n.message for n in app._notifications]
            assert any("could not connect" in m for m in messages)
            accept_btn = app.screen.query_one("#accept-btn")
            assert accept_btn.disabled

    asyncio.run(scenario())


def test_propose_runs_in_a_worker_thread_with_a_busy_state(tmp_path, monkeypatch):
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
                assert release.wait(timeout=2), "test never released describe()"
                column = Column(
                    name="id", type="INT", nullable=False, category=Category.NUMERIC
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

            assert not propose_btn.disabled
            assert app.screen.sub_title is None
            labels = [str(cb.label) for cb in app.screen.query(Checkbox)]
            assert any("schema" in label for label in labels)

    asyncio.run(scenario())


def test_config_reload_failure_after_write_is_caught_not_crashed(tmp_path, monkeypatch):
    async def scenario():
        db = tmp_path / "data.db"
        _table(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))

        real_load_config = app_module.load_config
        calls = {"n": 0}

        def flaky_load_config(path):
            calls["n"] += 1
            if calls["n"] > 1:
                raise ValueError("bad config after write")
            return real_load_config(path)

        monkeypatch.setattr(app_module, "load_config", flaky_load_config)

        async with app.run_test() as pilot:
            await pilot.pause()
            previous_config = app.config

            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#source-select").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.click("#accept-btn")
            await pilot.pause()

            # Back on Home; the reload failed but the app did not crash,
            # and the stale config from before the write is kept rather
            # than being clobbered by a half-completed reload.
            assert not isinstance(app.screen, ConfigureScreen)
            assert app.config is previous_config

            messages = [n.message for n in app._notifications]
            assert any("bad config after write" in m for m in messages)

    asyncio.run(scenario())


def test_configure_screen_surfaces_target_file_among_several_included(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _table(db)

        checks_dir = tmp_path / "checks"
        checks_dir.mkdir()
        (checks_dir / "a.yaml").write_text("checks: []\n")
        (checks_dir / "b.yaml").write_text("checks: []\n")

        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
            "include: [checks/*.yaml]\n"
            "checks: []\n"
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

            # Several included files match: the proposal names the one
            # that Accept will actually write to, rather than writing
            # silently to whichever one happened to sort first.
            proposal_text = str(app.screen.query_one("#proposal-text").content)
            assert "a.yaml" in proposal_text
            assert "2 included files" in proposal_text

            await pilot.click("#accept-btn")
            await pilot.pause()

        a_data = yaml.safe_load((checks_dir / "a.yaml").read_text())
        b_data = yaml.safe_load((checks_dir / "b.yaml").read_text())
        assert a_data["checks"]
        assert b_data["checks"] == []

    asyncio.run(scenario())


def test_configure_screen_unknown_object_disables_accept(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _table(db)
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

            messages = [n.message for n in app._notifications]
            assert any("not found" in m for m in messages)
            accept_btn = app.screen.query_one("#accept-btn")
            assert accept_btn.disabled

        data = yaml.safe_load(cfg.read_text())
        assert data["checks"] == []

    asyncio.run(scenario())


def test_configure_screen_cancel_button_writes_nothing(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _table(db)
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

        data = yaml.safe_load(cfg.read_text())
        assert data["checks"] == []

    asyncio.run(scenario())


def test_configure_screen_escape_cancels_without_writing(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _table(db)
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

        data = yaml.safe_load(cfg.read_text())
        assert data["checks"] == []

    asyncio.run(scenario())


def test_configure_screen_trim_deselects_a_proposed_check(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _table(db)
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

            freshness_cb = next(
                cb for cb in app.screen.query(Checkbox) if "freshness" in str(cb.label)
            )
            assert freshness_cb.value is True  # proposed checks start selected
            freshness_cb.value = False

            await pilot.click("#accept-btn")
            await pilot.pause()

        data = yaml.safe_load(cfg.read_text())
        metrics = [c["metric"] for c in data["checks"]]
        assert "freshness" not in metrics
        assert "schema" in metrics
        assert "row_count" in metrics

    asyncio.run(scenario())


def test_configure_screen_offered_check_can_be_selected(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _table(db)
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

            # "sum" over the numeric "amount" column is offered but is not
            # part of the base proposed bundle, and starts unselected -- the
            # same default as the CLI wizard's "blank to skip".
            sum_cb = app.screen.query_one("#offered-amount-sum", Checkbox)
            assert sum_cb.value is False
            sum_cb.value = True

            await pilot.click("#accept-btn")
            await pilot.pause()

        data = yaml.safe_load(cfg.read_text())
        sum_check = next(c for c in data["checks"] if c["metric"] == "sum")
        assert sum_check["column"] == "amount"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "metric,build_table,column,set_value,default_text,expect_key,expect_value",
    [
        pytest.param(
            "null_rate",
            _table,
            "amount",
            None,
            "0.05",
            "max",
            0.05,
            id="null_rate-default",
        ),
        pytest.param(
            "null_rate",
            _table,
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
def test_configure_screen_offered_check_value_is_written(
    tmp_path,
    metric,
    build_table,
    column,
    set_value,
    default_text,
    expect_key,
    expect_value,
):
    """The threshold Input beside an offered null_rate/freshness checkbox is
    pre-filled with the CLI wizard's own default for that metric (asserted
    before touching it, so the default-value coverage from the un-parametrized
    version survives), and Accept writes whichever value sits in the Input at
    that point -- the untouched default, or a value typed over it."""

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

            value_input = app.screen.query_one(f"#offered-value-{column}-{metric}")
            assert value_input.value == default_text
            if set_value is not None:
                value_input.value = set_value

            app.screen.query_one(f"#offered-{column}-{metric}").value = True
            await pilot.click("#accept-btn")
            await pilot.pause()

        data = yaml.safe_load(cfg.read_text())
        check = next(
            c for c in data["checks"] if c["metric"] == metric and c["column"] == column
        )
        assert check["expect"][expect_key] == expect_value

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "metric,build_table,column,invalid_value",
    [
        pytest.param(
            "null_rate", _table, "amount", "not-a-number", id="null_rate-invalid"
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
    tmp_path, metric, build_table, column, invalid_value
):
    """An offered null_rate/freshness threshold that doesn't parse for its
    metric is reported back as an error toast -- the same value formats the
    CLI wizard's prompt accepts -- rather than being written, or crashing
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

            app.screen.query_one(
                f"#offered-value-{column}-{metric}"
            ).value = invalid_value
            app.screen.query_one(f"#offered-{column}-{metric}").value = True
            await pilot.click("#accept-btn")
            await pilot.pause()

            # Did not crash: still on the Configure screen, with an error
            # toast.
            assert isinstance(app.screen, ConfigureScreen)
            messages = [n.message for n in app._notifications]
            assert any(invalid_value in m for m in messages)

        data = yaml.safe_load(cfg.read_text())
        assert data["checks"] == []

    asyncio.run(scenario())


def test_configure_screen_does_not_offer_metric_already_proposed_for_column(
    tmp_path,
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

            # ``modified_at`` already has a proposed freshness checkbox --
            # offering it a second time would collide on check_id and get
            # silently dropped by append_checks's dedup, so no offered
            # checkbox exists for it.
            assert not app.screen.query("#offered-modified_at-freshness")
            # ``event_time`` is temporal but wasn't auto-proposed, so it's
            # still legitimately offered.
            assert app.screen.query("#offered-event_time-freshness")

    asyncio.run(scenario())


def test_configure_screen_deselecting_everything_writes_nothing(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _table(db)
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

            # Nothing was selected, so Accept is a no-op: still on Configure.
            assert isinstance(app.screen, ConfigureScreen)

        data = yaml.safe_load(cfg.read_text())
        assert data["checks"] == []

    asyncio.run(scenario())


def test_configure_screen_accept_notifies_when_everything_dedups_away(tmp_path):
    """Every check the table would propose already exists in the config
    (same source/object/metric/column identity, regardless of threshold),
    so Accept's dedup skips all of them and nothing new is written. Accept
    must still tell the user that, rather than dismissing silently as if
    something had been saved."""

    async def scenario():
        db = tmp_path / "data.db"
        _table(db)
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

            # Every proposed checkbox starts checked, and every one of
            # them collides with an existing check -- select everything
            # as-is and accept it.
            await pilot.click("#accept-btn")
            await pilot.pause()

            messages = [n.message for n in app._notifications]
            assert any("0" in m and "wrote" in m for m in messages)
            assert any("4" in m and "skipped" in m for m in messages)

        data = yaml.safe_load(cfg.read_text())
        assert len(data["checks"]) == 4

    asyncio.run(scenario())


def test_configure_screen_propose_and_accept_preserve_manually_tuned_checks(
    tmp_path,
):
    """The Configure screen must open/propose cleanly against a config that
    already carries manually-tuned checks (non-default thresholds someone
    edited by hand) for the very object being configured, and Accept must
    not silently overwrite the tuned value. A freshly proposed freshness
    check on the same column collides on check_id with the existing
    hand-tuned one -- identity deliberately ignores `expect` -- and is
    skipped by append_checks's dedup, leaving the tuned threshold intact."""

    async def scenario():
        db = tmp_path / "data.db"
        _table(db)
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

            # The offered null_rate threshold Input still defaults to the
            # CLI wizard's own default -- the wizard never reads existing
            # check values back out of the config to seed a default.
            value_input = app.screen.query_one("#offered-value-amount-null_rate")
            assert value_input.value == "0.05"

            accept_btn = app.screen.query_one("#accept-btn")
            assert not accept_btn.disabled
            await pilot.click("#accept-btn")
            await pilot.pause()
            assert not isinstance(app.screen, ConfigureScreen)

        data = yaml.safe_load(cfg.read_text())
        freshness_checks = [
            c
            for c in data["checks"]
            if c["metric"] == "freshness" and c["column"] == "modified_at"
        ]
        # The proposed freshness (default 24h) collided on identity with
        # the existing hand-tuned one and was skipped -- the tuned value
        # survives rather than being duplicated or overwritten.
        assert len(freshness_checks) == 1
        assert freshness_checks[0]["expect"]["max_lag"] == "2h"

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


def test_configure_screen_shows_no_existing_checks_placeholder(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _table(db)
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

            existing = app.screen.query_one("#existing-checks")
            assert "(none yet)" in str(existing.children[0].render())

    asyncio.run(scenario())


def test_configure_screen_existing_check_input_prefilled_with_current_value(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _table(db)
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

            value_input = app.screen.query_one("#existing-value-0")
            assert value_input.value == "100"

    asyncio.run(scenario())


def test_configure_screen_between_operator_check_is_read_only(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _table(db)
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

            # Only the row_count/max check (index 0) is editable -- the
            # row_count/between check has no single-scalar operand to edit.
            with pytest.raises(NoMatches):
                app.screen.query_one("#existing-value-1")
            with pytest.raises(NoMatches):
                app.screen.query_one("#existing-save-1")

    asyncio.run(scenario())


def test_configure_screen_save_existing_check_rewrites_expect_on_disk(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _table(db)
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

            app.screen.query_one("#existing-value-0").value = "500"
            app.screen.query_one("#existing-save-0", Button).press()
            await pilot.pause()

        data = yaml.safe_load(cfg.read_text())
        row_count_checks = [
            c
            for c in data["checks"]
            if c["metric"] == "row_count" and "max" in c["expect"]
        ]
        assert row_count_checks[0]["expect"]["max"] == 500.0
        # The other check is untouched.
        between_checks = [c for c in data["checks"] if "between" in c["expect"]]
        assert between_checks[0]["expect"]["between"] == [1, 1000]

    asyncio.run(scenario())


def test_configure_screen_save_existing_check_does_not_change_check_id(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _table(db)
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

            app.screen.query_one("#existing-value-0").value = "500"
            app.screen.query_one("#existing-save-0", Button).press()
            await pilot.pause()

        config = load_config(cfg)
        matches = [
            c
            for c in config.checks
            if c.metric == "row_count" and c.expect.operator == "max"
        ]
        assert len(matches) == 1  # still one check, not duplicated or forked

    asyncio.run(scenario())


def test_configure_screen_save_existing_check_invalid_value_notifies_and_keeps_disk(
    tmp_path,
):
    async def scenario():
        db = tmp_path / "data.db"
        _table(db)
        cfg = _config_with_existing_checks(tmp_path / "config.yaml", db)
        original_text = cfg.read_text()

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

            app.screen.query_one("#existing-value-0").value = "not-a-number"
            app.screen.query_one("#existing-save-0", Button).press()
            await pilot.pause()

            messages = [n.message for n in app._notifications]
            assert any("not a number" in m for m in messages)

        assert cfg.read_text() == original_text

    asyncio.run(scenario())


def test_configure_screen_cancel_after_existing_edit_still_reloads_home(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _table(db)
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

            app.screen.query_one("#existing-value-0").value = "500"
            app.screen.query_one("#existing-save-0", Button).press()
            await pilot.pause()

            await pilot.click("#cancel-btn")
            await pilot.pause()
            assert not isinstance(app.screen, ConfigureScreen)

        data = yaml.safe_load(cfg.read_text())
        row_count_checks = [
            c
            for c in data["checks"]
            if c["metric"] == "row_count" and "max" in c["expect"]
        ]
        assert row_count_checks[0]["expect"]["max"] == 500.0

    asyncio.run(scenario())


# -- proposed-check threshold input (df-cpj) -------------------------------
#
# _table's schema (id PK, amount REAL, modified_at TIMESTAMP) always
# proposes in the same order: schema(0), row_count(1), freshness(2),
# duplicate_count(3) -- so "proposed-value-2" is freshness's Input across
# these tests, matching the fixture's fixed shape.


def test_configure_screen_proposed_freshness_has_a_value_input_prefilled_with_default(
    tmp_path,
):
    async def scenario():
        db = tmp_path / "data.db"
        _table(db)
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

            assert app.screen._proposed[2]["metric"] == "freshness"
            value_input = app.screen.query_one("#proposed-value-2")
            assert value_input.value == "24h"

    asyncio.run(scenario())


def test_configure_screen_non_freshness_proposed_checks_have_no_value_input(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _table(db)
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


def test_configure_screen_accept_uses_edited_proposed_freshness_value(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _table(db)
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

            app.screen.query_one("#proposed-value-2").value = "48h"
            await pilot.click("#accept-btn")
            await pilot.pause()
            assert not isinstance(app.screen, ConfigureScreen)

        data = yaml.safe_load(cfg.read_text())
        freshness_checks = [c for c in data["checks"] if c["metric"] == "freshness"]
        assert len(freshness_checks) == 1
        assert freshness_checks[0]["expect"]["max_lag"] == "48h"

    asyncio.run(scenario())


def test_configure_screen_accept_invalid_proposed_freshness_value_writes_nothing(
    tmp_path,
):
    async def scenario():
        db = tmp_path / "data.db"
        _table(db)
        cfg = _config(tmp_path / "config.yaml", db)
        original_text = cfg.read_text()

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

            app.screen.query_one("#proposed-value-2").value = "not-a-duration"
            await pilot.click("#accept-btn")
            await pilot.pause()

            # Errors keep the screen open rather than dismissing with a
            # partially-accepted bundle.
            assert isinstance(app.screen, ConfigureScreen)
            messages = [n.message for n in app._notifications]
            assert any("invalid max lag" in m for m in messages)

        assert cfg.read_text() == original_text

    asyncio.run(scenario())


def test_configure_screen_unchecking_proposed_freshness_ignores_its_value(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _table(db)
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

            app.screen.query_one("#proposed-value-2").value = "not-a-duration"
            app.screen.query_one("#proposed-2-freshness", Checkbox).value = False
            await pilot.click("#accept-btn")
            await pilot.pause()
            assert not isinstance(app.screen, ConfigureScreen)

        data = yaml.safe_load(cfg.read_text())
        assert not any(c["metric"] == "freshness" for c in data["checks"])

    asyncio.run(scenario())
