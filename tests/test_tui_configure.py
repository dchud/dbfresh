import asyncio

import yaml
from textual.widgets import Checkbox

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
            app.screen.query_one("#source-input").value = "s"
            app.screen.query_one("#object-input").value = "fct"

            await pilot.click("#propose-btn")
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

            app.screen.query_one("#source-input").value = "s"
            app.screen.query_one("#object-input").value = "t"
            await pilot.click("#propose-btn")
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

            app.screen.query_one("#source-input").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
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

            app.screen.query_one("#source-input").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
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

            app.screen.query_one("#source-input").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
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


def test_configure_screen_offered_null_rate_defaults_to_cli_default(tmp_path):
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

            # The threshold Input beside the offered null_rate checkbox is
            # pre-filled with the CLI wizard's own default.
            value_input = app.screen.query_one("#offered-value-amount-null_rate")
            assert value_input.value == "0.05"

            app.screen.query_one("#offered-amount-null_rate").value = True
            await pilot.click("#accept-btn")
            await pilot.pause()

        data = yaml.safe_load(cfg.read_text())
        null_rate = next(
            c
            for c in data["checks"]
            if c["metric"] == "null_rate" and c["column"] == "amount"
        )
        assert null_rate["expect"]["max"] == 0.05

    asyncio.run(scenario())


def test_configure_screen_offered_null_rate_custom_value_is_written(tmp_path):
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

            app.screen.query_one("#offered-value-amount-null_rate").value = "0.2"
            app.screen.query_one("#offered-amount-null_rate").value = True
            await pilot.click("#accept-btn")
            await pilot.pause()

        data = yaml.safe_load(cfg.read_text())
        null_rate = next(
            c
            for c in data["checks"]
            if c["metric"] == "null_rate" and c["column"] == "amount"
        )
        assert null_rate["expect"]["max"] == 0.2

    asyncio.run(scenario())


def test_configure_screen_offered_null_rate_invalid_value_shows_note(tmp_path):
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

            app.screen.query_one(
                "#offered-value-amount-null_rate"
            ).value = "not-a-number"
            app.screen.query_one("#offered-amount-null_rate").value = True
            await pilot.click("#accept-btn")
            await pilot.pause()

            # Did not crash: still on the Configure screen, with a note.
            assert isinstance(app.screen, ConfigureScreen)
            proposal_text = str(app.screen.query_one("#proposal-text").content)
            assert "not-a-number" in proposal_text

        data = yaml.safe_load(cfg.read_text())
        assert data["checks"] == []

    asyncio.run(scenario())


def test_configure_screen_offered_freshness_defaults_to_cli_default(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _table_with_offered_temporal(db)
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

            # ``event_time`` is temporal but unconventionally named, so it's
            # only ever offered, never auto-proposed -- unlike ``modified_at``,
            # its offered freshness checkbox has no proposed counterpart to
            # collide with. The threshold Input beside it is pre-filled with
            # the CLI wizard's own default.
            value_input = app.screen.query_one("#offered-value-event_time-freshness")
            assert value_input.value == "24h"

            app.screen.query_one("#offered-event_time-freshness").value = True
            await pilot.click("#accept-btn")
            await pilot.pause()

        data = yaml.safe_load(cfg.read_text())
        freshness = next(
            c
            for c in data["checks"]
            if c["metric"] == "freshness" and c["column"] == "event_time"
        )
        assert freshness["expect"]["max_lag"] == "24h"

    asyncio.run(scenario())


def test_configure_screen_offered_freshness_custom_value_is_written(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _table_with_offered_temporal(db)
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

            app.screen.query_one("#offered-value-event_time-freshness").value = "6h"
            app.screen.query_one("#offered-event_time-freshness").value = True
            await pilot.click("#accept-btn")
            await pilot.pause()

        data = yaml.safe_load(cfg.read_text())
        freshness = next(
            c
            for c in data["checks"]
            if c["metric"] == "freshness" and c["column"] == "event_time"
        )
        assert freshness["expect"]["max_lag"] == "6h"

    asyncio.run(scenario())


def test_configure_screen_offered_freshness_invalid_value_shows_note(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _table_with_offered_temporal(db)
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

            app.screen.query_one(
                "#offered-value-event_time-freshness"
            ).value = "not-a-duration"
            app.screen.query_one("#offered-event_time-freshness").value = True
            await pilot.click("#accept-btn")
            await pilot.pause()

            # Did not crash: still on the Configure screen, with a note.
            assert isinstance(app.screen, ConfigureScreen)
            proposal_text = str(app.screen.query_one("#proposal-text").content)
            assert "not-a-duration" in proposal_text

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

            app.screen.query_one("#source-input").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
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

            app.screen.query_one("#source-input").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
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

            app.screen.query_one("#source-input").value = "s"
            app.screen.query_one("#object-input").value = "fct"
            await pilot.click("#propose-btn")
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
