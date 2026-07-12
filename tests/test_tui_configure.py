import asyncio

import yaml

from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.tui.app import DbfreshApp
from dbfresh.tui.configure import ConfigureScreen


def _table(db):
    adapter = SqliteAdapter(str(db))
    adapter.rows(
        "CREATE TABLE fct (id INTEGER PRIMARY KEY, amount REAL, modified_at TIMESTAMP)"
    )
    adapter.close()


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
