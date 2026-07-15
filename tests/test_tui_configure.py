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


def test_configure_preselects_a_lone_source(tmp_path):
    """With exactly one configured source, Configure preselects it in the
    source Select, so a single-source project needs no dropdown interaction
    before Propose."""

    async def scenario():
        db = tmp_path / "data.db"
        _table(db)
        cfg = _config(tmp_path / "config.yaml", db)  # exactly one source, "s"

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert app.screen.query_one("#source-select").value == "s"

    asyncio.run(scenario())


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


def test_accept_fires_a_run_so_new_checks_show_results_without_pressing_r(tmp_path):
    """Nothing connects "just configured a check" to "see it run" other
    than the user finding the 'r' key on their own -- Accept must wire the
    two together itself, firing the existing Run action once Home has
    reloaded the config that Accept just wrote."""

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

            # Accept dismissed Configure; Home reloaded the config and, on
            # its own, started a run -- wait for that background worker
            # (never pressed 'r' in this scenario) before asserting on it.
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            assert app.last_run is not None
            table = app.query_one("#dashboard-grid", DataTable)
            row_keys = {key.value for key in table.rows}
            assert "s\x1ffct" in row_keys
            # row_count is one of the checks Accept just wrote for s.fct;
            # a fresh run gives it a real status rather than "never
            # observed" ('·').
            cell = table.get_cell("s\x1ffct", "overall")
            assert cell.plain != "·"

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


# -- new-source form (df-ymt) ----------------------------------------------


def test_configure_screen_opens_straight_into_new_source_form_at_zero_sources(
    tmp_path,
):
    async def scenario():
        cfg = tmp_path / "config.yaml"
        cfg.write_text("sources: {}\nchecks: []\n")

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            # No sources to propose against -- the propose form (an empty
            # Select and nothing else) would be a dead end, so the screen
            # opens straight into the new-source form instead.
            assert app.screen.query_one("#new-source-form").display
            assert not app.screen.query_one("#propose-section").display

    asyncio.run(scenario())


def test_configure_screen_new_source_button_reveals_form_when_sources_exist(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _table(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            # A source already exists -- the propose form opens by
            # default, with the new-source form reachable on demand.
            assert app.screen.query_one("#propose-section").display
            assert not app.screen.query_one("#new-source-form").display

            await pilot.click("#new-source-btn")
            await pilot.pause()

            assert app.screen.query_one("#new-source-form").display
            assert not app.screen.query_one("#propose-section").display

    asyncio.run(scenario())


def test_configure_screen_new_source_probe_success_adds_and_selects_source(tmp_path):
    """A probe that succeeds writes the source to disk (via
    ``configurator.add_source``, reused verbatim), reflects it into the
    Select, and returns to the propose form with it selected and already
    usable for Propose in the same session -- no reopen or reload needed.
    """

    async def scenario():
        db = tmp_path / "data.db"
        _table(db)
        cfg = tmp_path / "config.yaml"
        cfg.write_text("sources: {}\nchecks: []\n")

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#new-source-name-input").value = "s"
            app.screen.query_one("#new-source-type-input").value = "sqlite"
            app.screen.query_one("#new-source-params").text = f"database={db}"

            await pilot.click("#new-source-add-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            assert not app.screen.query_one("#new-source-form").display
            assert app.screen.query_one("#propose-section").display
            select = app.screen.query_one("#source-select")
            assert select.value == "s"

            data = yaml.safe_load(cfg.read_text())
            assert data["sources"]["s"]["type"] == "sqlite"
            assert data["sources"]["s"]["database"] == str(db)

            # Usable for Propose right away, in this same screen instance.
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            labels = [str(cb.label) for cb in app.screen.query(Checkbox)]
            assert any("row_count" in label for label in labels)

    asyncio.run(scenario())


def test_new_source_with_env_var_param_is_resolved_in_memory_for_immediate_use(
    tmp_path, monkeypatch
):
    """A new source added with a ${VAR} param keeps ${VAR} in the YAML but
    resolves it in memory, so an immediate Propose in the same session
    connects with the real value -- not a literal "${VAR}". The form itself
    recommends key=${VAR} for secrets, so this is the path a secret-using
    user actually takes.
    """

    async def scenario():
        db = tmp_path / "data.db"
        _table(db)
        monkeypatch.setenv("DBFRESH_TEST_DB", str(db))
        cfg = tmp_path / "config.yaml"
        cfg.write_text("sources: {}\nchecks: []\n")

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#new-source-name-input").value = "s"
            app.screen.query_one("#new-source-type-input").value = "sqlite"
            params = app.screen.query_one("#new-source-params")
            params.text = "database=${DBFRESH_TEST_DB}"

            await pilot.click("#new-source-add-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            # Disk keeps the raw ${VAR} -- no resolved secret written to YAML.
            data = yaml.safe_load(cfg.read_text())
            assert data["sources"]["s"]["database"] == "${DBFRESH_TEST_DB}"
            # In memory it is resolved, so the source is immediately usable.
            assert app.screen._config.sources["s"].params["database"] == str(db)

            # Propose against the just-added source connects with the real
            # path; a literal "${DBFRESH_TEST_DB}" would find no fct table.
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            labels = [str(cb.label) for cb in app.screen.query(Checkbox)]
            assert any("row_count" in label for label in labels)

    asyncio.run(scenario())


def test_configure_screen_new_source_probe_failure_shows_toast_and_keeps_form_open(
    tmp_path, monkeypatch
):
    async def scenario():
        monkeypatch.setitem(factory._ADAPTERS, "unreachable", _FakeUnreachableAdapter)

        cfg = tmp_path / "config.yaml"
        cfg.write_text("sources: {}\nchecks: []\n")

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#new-source-name-input").value = "s"
            app.screen.query_one("#new-source-type-input").value = "unreachable"

            await pilot.click("#new-source-add-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            # Did not crash: still on Configure, in the new-source form
            # (not silently dropped back to the propose form), with an
            # error toast -- the same notify() channel Propose's own
            # connect failure uses.
            assert isinstance(app.screen, ConfigureScreen)
            assert app.screen.query_one("#new-source-form").display
            messages = [n.message for n in app._notifications]
            assert any("could not connect" in m for m in messages)

        data = yaml.safe_load(cfg.read_text())
        assert data["sources"] == {}

    asyncio.run(scenario())


def test_configure_screen_new_source_duplicate_name_is_rejected_before_probing(
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

            await pilot.click("#new-source-btn")
            await pilot.pause()

            app.screen.query_one("#new-source-name-input").value = "s"  # already exists
            app.screen.query_one("#new-source-type-input").value = "sqlite"

            await pilot.click("#new-source-add-btn")
            await pilot.pause()

            # Rejected synchronously, before any worker (and therefore any
            # network probe) ever started.
            assert not app.workers
            messages = [n.message for n in app._notifications]
            assert any("already exists" in m for m in messages)

    asyncio.run(scenario())


def test_new_source_flow_works_when_config_file_did_not_exist_yet(tmp_path):
    """The full first-run path: ``dbfresh ui`` against a config path that
    doesn't exist yet starts ``DbfreshApp`` against an empty in-memory
    ``Config`` (see ``cli._ui_command`` / test_cli_ui.py's missing-config
    test for the CLI side of this) -- Configure's new-source form must
    still work end to end from there, creating ``config.yaml`` for the
    first time via :func:`~dbfresh.configurator.add_source`.
    """

    async def scenario():
        from dbfresh.config import Config

        db = tmp_path / "data.db"
        _table(db)
        cfg = tmp_path / "config.yaml"
        assert not cfg.exists()

        initial_config = Config(sources={}, checks=[], config_dir=tmp_path)
        app = DbfreshApp(
            config_path=cfg,
            store_path=str(tmp_path / "obs.db"),
            initial_config=initial_config,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            app.screen.query_one("#new-source-name-input").value = "s"
            app.screen.query_one("#new-source-type-input").value = "sqlite"
            app.screen.query_one("#new-source-params").text = f"database={db}"

            await pilot.click("#new-source-add-btn")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            assert cfg.exists()  # written for the first time by add_source
            select = app.screen.query_one("#source-select")
            assert select.value == "s"

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
                assert release.wait(timeout=2), "test never released describe()"
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
