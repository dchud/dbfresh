import asyncio

import yaml

from dbfresh.adapters import factory
from dbfresh.adapters.base import Category, Column, ObjectInfo
from dbfresh.adapters.databricks import DatabricksDialect
from dbfresh.adapters.sqlite import SqliteAdapter
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
            app.screen.query_one("#source-input").value = "s"
            app.screen.query_one("#object-input").value = "fct"

            await pilot.click("#propose-btn")
            await pilot.pause()

            proposal_text = str(app.screen.query_one("#proposal-text").content)
            assert "row_count" in proposal_text
            assert "schema" in proposal_text
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

            app.screen.query_one("#source-input").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
            await pilot.pause()
            await pilot.click("#accept-btn")
            await pilot.pause()

            tree = app.query_one("#dashboard-tree")
            source_names = [str(n.label).split(" ")[0] for n in tree.root.children]
            assert "s" in source_names

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

            app.screen.query_one("#source-input").value = "s"
            app.screen.query_one("#object-input").value = "v"
            await pilot.click("#propose-btn")
            await pilot.pause()

            proposal_text = str(app.screen.query_one("#proposal-text").content)
            assert "freshness" not in proposal_text

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

            app.screen.query_one("#source-input").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
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

            app.screen.query_one("#source-input").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            app.screen.query_one("#timestamp-input").value = "updated_at"
            await pilot.click("#propose-btn")
            await pilot.pause()

            proposal_text = str(app.screen.query_one("#proposal-text").content)
            assert "freshness" in proposal_text

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

            app.screen.query_one("#source-input").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
            await pilot.pause()

            # Did not crash: still on the Configure screen.
            assert isinstance(app.screen, ConfigureScreen)
            proposal_text = str(app.screen.query_one("#proposal-text").content)
            assert "could not connect" in proposal_text
            accept_btn = app.screen.query_one("#accept-btn")
            assert accept_btn.disabled

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

            app.screen.query_one("#source-input").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
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

            app.screen.query_one("#source-input").value = "s"
            app.screen.query_one("#object-input").value = "does_not_exist"
            await pilot.click("#propose-btn")
            await pilot.pause()

            proposal_text = str(app.screen.query_one("#proposal-text").content)
            assert "not found" in proposal_text
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

            app.screen.query_one("#source-input").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
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
