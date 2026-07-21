import asyncio
import re
import subprocess
import threading
from datetime import UTC, datetime

from textual.widgets import DataTable, Static

from dbfresh import runner
from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.checks import Check, check_id
from dbfresh.config import load_config_tolerant
from dbfresh.engine import Result, Status
from dbfresh.store import Store
from dbfresh.tui.app import DbfreshApp
from dbfresh.tui.dashboard import header_key, is_header_key
from dbfresh.tui.screens import HistoryScreen, ObjectDetailScreen, ReportScreen

_OBJECT_ROW_KEY = (
    "s\x1ft"  # source "s", object "t" -- matches GridRow.key's shape
)


def _seed_db(path):
    adapter = SqliteAdapter(str(path))
    adapter.rows("CREATE TABLE t (id INTEGER, email TEXT)")
    adapter.rows(
        "INSERT INTO t (id, email) VALUES (1, 'a@x.com'), (2, NULL), (3, NULL)"
    )
    adapter.close()


def _config(path, db):
    path.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
        "checks:\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: row_count\n"
        "    expect: { between: [1, 10] }\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: null_rate\n"
        "    column: email\n"
        "    expect: { max: 0.1 }\n"
    )
    return path


def _row_count_check():
    return Check(source="s", object="t", metric="row_count")


def _null_rate_check():
    return Check(source="s", object="t", metric="null_rate", column="email")


def _overall_glyph(table, row_key):
    return table.get_cell(row_key, "overall").plain


def _multi_object_config(path):
    """Three objects on one source, no real DB behind any of them -- these
    scenarios only ever seed the store directly and never press 'r', so no
    adapter connection is ever attempted."""
    path.write_text(
        'sources:\n  s: { type: sqlite, database: "unused.db" }\n'
        "checks:\n"
        "  - source: s\n"
        "    object: orders\n"
        "    metric: row_count\n"
        "    expect: { between: [1, 10] }\n"
        "  - source: s\n"
        "    object: items\n"
        "    metric: row_count\n"
        "    expect: { between: [1, 10] }\n"
        "  - source: s\n"
        "    object: archive\n"
        "    metric: row_count\n"
        "    expect: { between: [1, 10] }\n"
    )
    return path


def _seed_status(store, object_, status, source="s"):
    check = Check(source=source, object=object_, metric="row_count")
    run_id = store.start_run()
    store.record_observation(
        run_id,
        Result(
            object=object_,
            metric="row_count",
            status=status,
            source=source,
            value=1,
            check_id=check_id(check),
        ),
    )
    store.finish_run(run_id, status)


def _row_order(table):
    return [row.key.value for row in table.ordered_rows]


def test_dashboard_reflects_seeded_store_statuses_on_mount(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"
        store = Store(store_path)
        run_id = store.start_run()
        store.record_observation(
            run_id,
            Result(
                object="t",
                metric="row_count",
                status=Status.OK,
                source="s",
                value=3,
                check_id=check_id(_row_count_check()),
            ),
        )
        store.record_observation(
            run_id,
            Result(
                object="t",
                metric="null_rate",
                status=Status.FAIL,
                source="s",
                value=0.9,
                check_id=check_id(_null_rate_check()),
            ),
        )
        store.finish_run(run_id, Status.FAIL)
        store.close()

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#dashboard-grid", DataTable)
            # A source header row for "s", plus one object row for s.t
            # (object-scope), rolled up to the worst of its two checks.
            assert table.row_count == 2
            assert _overall_glyph(table, _OBJECT_ROW_KEY) == "✗"  # FAIL

            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ObjectDetailScreen)
            detail_table = app.screen.query_one(DataTable)
            assert detail_table.row_count == 2
            assert (
                _overall_glyph(detail_table, check_id(_row_count_check()))
                == "✓"
            )
            assert (
                _overall_glyph(detail_table, check_id(_null_rate_check()))
                == "✗"
            )

    asyncio.run(scenario())


def test_home_shows_empty_state_when_no_checks_are_configured(tmp_path):
    async def scenario():
        cfg = tmp_path / "config.yaml"
        cfg.write_text("sources: {}\nchecks: []\n")

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()

            # A blank grid (just the header row) reads as broken to a
            # first-time user -- guidance is shown instead, and the grid
            # and legend (which would otherwise be an empty wall) are
            # hidden rather than shown alongside it.
            empty_state = app.query_one("#empty-state", Static)
            assert empty_state.display
            assert "press 'c'" in str(empty_state.render())

            table = app.query_one("#dashboard-grid", DataTable)
            assert not table.display
            assert not app.query_one("#status-legend", Static).display

    asyncio.run(scenario())


def test_home_hides_empty_state_once_checks_exist(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()

            assert not app.query_one("#empty-state", Static).display
            table = app.query_one("#dashboard-grid", DataTable)
            assert table.display
            assert app.query_one("#status-legend", Static).display

    asyncio.run(scenario())


def test_home_hides_last_run_line_with_no_completed_run(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            assert not app.query_one("#last-run-line", Static).display

    asyncio.run(scenario())


def test_home_shows_last_run_line_after_a_completed_run(tmp_path, pump_until):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            assert not app.query_one("#last-run-line", Static).display

            await pilot.press("r")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot, lambda: app.query_one("#last-run-line", Static).display
            )

            # row_count passes, null_rate fails -- see _seed_db / _config.
            widget = app.query_one("#last-run-line", Static)
            assert widget.display
            text = str(widget.render())
            assert "last run:" in text
            assert "2 checks" in text
            assert "1 failed" in text

    asyncio.run(scenario())


def test_unobserved_line_shows_count_when_checks_have_no_observations(
    tmp_path,
):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            widget = app.query_one("#unobserved-line", Static)
            assert widget.display
            assert "not yet run on this machine" in str(widget.render())

    asyncio.run(scenario())


def test_unobserved_line_hidden_after_refresh_once_every_check_is_observed(
    tmp_path,
):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#unobserved-line", Static).display

            store = app._require_store()
            _seed_status(store, "t", Status.OK)
            run_id = store.start_run()
            store.record_observation(
                run_id,
                Result(
                    object="t",
                    metric="null_rate",
                    status=Status.OK,
                    source="s",
                    value=0.0,
                    check_id=check_id(_null_rate_check()),
                ),
            )
            store.finish_run(run_id, Status.OK)

            app.refresh_dashboard()
            await pilot.pause()
            assert not app.query_one("#unobserved-line", Static).display

    asyncio.run(scenario())


def test_reload_toast_appends_unobserved_count_when_checks_are_unobserved(
    tmp_path, pump_until
):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()

            await pilot.press("R")
            await pilot.pause()
            await pump_until(
                pilot,
                lambda: any(
                    "config reloaded" in n.message for n in app._notifications
                ),
            )

            messages = [n.message for n in app._notifications]
            assert any(
                "config reloaded · 2 checks not yet run on this machine" in m
                for m in messages
            )

    asyncio.run(scenario())


def _undefined_var_config(path, db):
    path.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
        '  broken: { type: sqlite, database: "${DB_PASSWORD}" }\n'
        "checks:\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: row_count\n"
        "    expect: { between: [1, 10] }\n"
    )
    return path


def test_missing_secrets_banner_shows_names_and_where_to_set_them(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg_path = _undefined_var_config(tmp_path / "config.yaml", db)
        config, missing = load_config_tolerant(cfg_path, env={})

        app = DbfreshApp(
            config_path=cfg_path,
            store_path=str(tmp_path / "obs.db"),
            initial_config=config,
            missing_secrets=missing,
        )
        async with app.run_test() as pilot:
            await pilot.pause()

            banner = app.query_one("#missing-secrets-banner", Static)
            assert banner.display
            text = str(banner.render())
            assert "DB_PASSWORD" in text
            assert "config.yaml" in text

            # The literal ${VAR} token stays in place rather than being
            # resolved -- the app started instead of refusing to launch.
            table = app.query_one("#dashboard-grid", DataTable)
            assert table.display

    asyncio.run(scenario())


def test_missing_secrets_banner_hidden_when_nothing_missing(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()

            banner = app.query_one("#missing-secrets-banner", Static)
            assert not banner.display

    asyncio.run(scenario())


def _isolate_git_config(tmp_path, monkeypatch):
    # _env_at_risk runs real git (env_hygiene.committable_env_file) -- pin
    # its config away from the developer's own, so a global gitignore that
    # happens to ignore .env can't make these tests pass or fail depending
    # on machine layout.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "no-system"))


def _git_init(repo_dir):
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)


def test_env_hygiene_banner_shown_when_env_file_is_not_gitignored(
    tmp_path, monkeypatch
):
    async def scenario():
        _isolate_git_config(tmp_path, monkeypatch)
        _git_init(tmp_path)
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        (tmp_path / ".env").write_text("DB_PASSWORD=x\n")

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()

            banner = app.query_one("#env-hygiene-banner", Static)
            assert banner.display
            text = str(banner.render())
            assert "not gitignored" in text

    asyncio.run(scenario())


def test_env_hygiene_banner_hidden_when_env_file_is_gitignored(
    tmp_path, monkeypatch
):
    async def scenario():
        _isolate_git_config(tmp_path, monkeypatch)
        _git_init(tmp_path)
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        (tmp_path / ".env").write_text("DB_PASSWORD=x\n")
        (tmp_path / ".gitignore").write_text(".env\n")

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()

            banner = app.query_one("#env-hygiene-banner", Static)
            assert not banner.display

    asyncio.run(scenario())


def test_env_hygiene_banner_hidden_when_no_env_file(tmp_path, monkeypatch):
    async def scenario():
        _isolate_git_config(tmp_path, monkeypatch)
        _git_init(tmp_path)
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()

            banner = app.query_one("#env-hygiene-banner", Static)
            assert not banner.display

    asyncio.run(scenario())


def test_reload_tolerates_missing_secrets_when_no_initial_config(
    tmp_path, monkeypatch
):
    """The no-initial-config path -- on_mount's own _reload_config -- loads
    tolerantly: an unset ${VAR} leaves the app running with the banner rather
    than raising on reload (a plain load would raise on mount here)."""

    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _undefined_var_config(tmp_path / "config.yaml", db)
        monkeypatch.delenv("DB_PASSWORD", raising=False)

        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await pilot.pause()
            assert "DB_PASSWORD" in app.missing_secrets
            assert app.query_one("#missing-secrets-banner", Static).display

    asyncio.run(scenario())


def test_on_mount_reuses_a_preloaded_config_without_reparsing(
    tmp_path, monkeypatch
):
    async def scenario():
        from dbfresh.config import load_config as real_load_config

        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        preloaded = real_load_config(cfg)

        def fail_if_called(path):
            raise AssertionError(
                "load_config_tolerant must not run again at mount time"
            )

        monkeypatch.setattr(
            "dbfresh.tui.app.load_config_tolerant", fail_if_called
        )

        app = DbfreshApp(
            config_path=cfg,
            store_path=str(store_path),
            initial_config=preloaded,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.config is preloaded

    asyncio.run(scenario())


def test_run_action_updates_dashboard_from_new_observations(
    tmp_path, pump_until
):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#dashboard-grid", DataTable)

            # Nothing observed yet: the object row's overall is unknown.
            assert _overall_glyph(table, _OBJECT_ROW_KEY) == "·"

            await pilot.press("r")
            # The Run action starts the check run on a background worker;
            # wait for it to finish before asserting on its results.
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot,
                lambda: (
                    _overall_glyph(
                        app.query_one("#dashboard-grid", DataTable),
                        _OBJECT_ROW_KEY,
                    )
                    == "✗"
                ),
            )

            table = app.query_one("#dashboard-grid", DataTable)
            assert (
                _overall_glyph(table, _OBJECT_ROW_KEY) == "✗"
            )  # null_rate fails

            await pilot.press("enter")
            await pilot.pause()
            detail_table = app.screen.query_one(DataTable)
            assert (
                _overall_glyph(detail_table, check_id(_row_count_check()))
                == "✓"
            )
            assert (
                _overall_glyph(detail_table, check_id(_null_rate_check()))
                == "✗"
            )

            assert app.last_run is not None
            assert app.last_run.status == Status.FAIL

    asyncio.run(scenario())


def test_run_action_stays_responsive_and_refreshes_when_the_worker_completes(
    tmp_path,
    monkeypatch,
    pump_until,
):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        started = threading.Event()
        release = threading.Event()
        real_run_and_persist = runner.run_and_persist

        def blocking_run_and_persist(
            config, store, now=None, only=None, object_=None, on_result=None
        ):
            started.set()
            assert release.wait(timeout=2), "test never released the run"
            return real_run_and_persist(
                config,
                store,
                now=now,
                only=only,
                object_=object_,
                on_result=on_result,
            )

        monkeypatch.setattr(
            runner, "run_and_persist", blocking_run_and_persist
        )

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()

            await pilot.press("r")
            await pilot.pause()

            # The worker thread is blocked inside the run, but the event
            # loop kept servicing messages meanwhile: the run has started,
            # the dashboard has not been refreshed yet, and the app is
            # still responsive to further queries.
            assert started.wait(timeout=2)
            assert app.last_run is None
            table = app.query_one("#dashboard-grid", DataTable)
            assert _overall_glyph(table, _OBJECT_ROW_KEY) == "·"

            release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(pilot, lambda: app.last_run is not None)

            assert app.last_run is not None
            assert app.last_run.status == Status.FAIL
            table = app.query_one("#dashboard-grid", DataTable)
            assert _overall_glyph(table, _OBJECT_ROW_KEY) == "✗"

    asyncio.run(scenario())


def test_run_action_error_notifies_and_leaves_app_alive(
    tmp_path, monkeypatch, pump_until
):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        def raising_run_and_persist(
            config, store, now=None, only=None, object_=None, on_result=None
        ):
            raise RuntimeError("store locked")

        monkeypatch.setattr(runner, "run_and_persist", raising_run_and_persist)

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()

            await pilot.press("r")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot,
                lambda: any(
                    "store locked" in n.message for n in app._notifications
                ),
            )

            # The app survived the worker error rather than being torn
            # down, and the dashboard/last_run are untouched.
            table = app.query_one("#dashboard-grid", DataTable)
            assert _overall_glyph(table, _OBJECT_ROW_KEY) == "·"
            assert app.last_run is None

            messages = [n.message for n in app._notifications]
            assert any("store locked" in m for m in messages)

    asyncio.run(scenario())


def test_run_action_success_toast_summarizes_counts(tmp_path, pump_until):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()

            await pilot.press("r")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot,
                lambda: any(
                    "1 ok" in n.message and "1 failed" in n.message
                    for n in app._notifications
                ),
            )

            # row_count passes (3 rows, between 1 and 10) and null_rate
            # fails (2 of 3 emails null, over the 0.1 max) -- see _seed_db
            # / _config.
            messages = [n.message for n in app._notifications]
            assert any("1 ok" in m and "1 failed" in m for m in messages)

    asyncio.run(scenario())


def test_run_action_wires_per_check_progress_into_the_header(
    tmp_path, monkeypatch, pump_until
):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        seen: list[tuple[int, int]] = []
        real_on_run_progress = DbfreshApp.on_run_progress

        def spy_on_run_progress(self, message):
            seen.append((message.count, message.total))
            real_on_run_progress(self, message)

        monkeypatch.setattr(DbfreshApp, "on_run_progress", spy_on_run_progress)

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()

            await pilot.press("r")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(pilot, lambda: seen == [(1, 2), (2, 2)])

            # Two checks configured on the same source, evaluated serially
            # on that source's one connection (see dbfresh.engine.run_checks)
            # -- one RunProgress message per completed check, counting up.
            assert seen == [(1, 2), (2, 2)]

    asyncio.run(scenario())


def test_run_action_second_press_cancels_first_with_a_notice(
    tmp_path, monkeypatch, pump_until
):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        started = threading.Event()
        release = threading.Event()
        blocked_once = threading.Event()
        blocked_once.set()
        real_run_and_persist = runner.run_and_persist

        def maybe_blocking_run_and_persist(
            config, store, now=None, only=None, object_=None, on_result=None
        ):
            if blocked_once.is_set():
                blocked_once.clear()
                started.set()
                assert release.wait(timeout=2), (
                    "test never released the first run"
                )
            return real_run_and_persist(
                config,
                store,
                now=now,
                only=only,
                object_=object_,
                on_result=on_result,
            )

        monkeypatch.setattr(
            runner, "run_and_persist", maybe_blocking_run_and_persist
        )

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()

            await pilot.press("r")
            await pilot.pause()
            assert started.wait(timeout=2)

            # A second press while the first run is still in flight cancels
            # it (exclusive worker group) rather than queuing or ignoring
            # the keypress -- surfaced as a notice instead of silently.
            await pilot.press("r")
            await pilot.pause()

            messages = [n.message for n in app._notifications]
            assert any("cancelled" in m for m in messages)

            release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(pilot, lambda: app.last_run is not None)

            # The second run still completed normally.
            assert app.last_run is not None
            assert app.last_run.status == Status.FAIL

    asyncio.run(scenario())


def test_run_action_refreshes_object_detail_screen_when_on_top(
    tmp_path, pump_until
):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")  # Home -> ObjectDetailScreen, s.t
            await pilot.pause()
            assert isinstance(app.screen, ObjectDetailScreen)
            detail_table = app.screen.query_one(DataTable)
            assert (
                _overall_glyph(detail_table, check_id(_row_count_check()))
                == "·"
            )

            await pilot.press("r")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot,
                lambda: (
                    _overall_glyph(
                        app.screen.query_one(DataTable),
                        check_id(_row_count_check()),
                    )
                    == "✓"
                ),
            )

            # Still on the same screen -- no esc + re-enter needed -- and
            # its grid reflects the run that just completed.
            assert isinstance(app.screen, ObjectDetailScreen)
            detail_table = app.screen.query_one(DataTable)
            assert (
                _overall_glyph(detail_table, check_id(_row_count_check()))
                == "✓"
            )
            assert (
                _overall_glyph(detail_table, check_id(_null_rate_check()))
                == "✗"
            )

    asyncio.run(scenario())


def test_run_action_refreshes_report_screen_when_on_top(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("r")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()

            assert isinstance(app.screen, ReportScreen)
            assert app.screen._run is app.last_run

            await pilot.press("r")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            # Still on the Report screen -- no esc + re-enter needed -- and
            # it now reflects the second run rather than the one it was
            # pushed with.
            assert isinstance(app.screen, ReportScreen)
            assert app.screen._run is app.last_run

    asyncio.run(scenario())


def test_selecting_a_check_row_opens_history_with_its_observations(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"
        store = Store(store_path)
        cid = check_id(_row_count_check())
        for value in (3, 5):
            run_id = store.start_run()
            store.record_observation(
                run_id,
                Result(
                    object="t",
                    metric="row_count",
                    status=Status.OK,
                    source="s",
                    value=value,
                    check_id=cid,
                ),
            )
            store.finish_run(run_id, Status.OK)
        store.close()

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            # Home grid: one row (s.t) -- enter drills into its checks.
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ObjectDetailScreen)

            # ObjectDetailScreen: row_count is first (config order) --
            # enter on the default cursor position opens its History.
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, HistoryScreen)
            text = str(app.screen.query_one("#history-text").content)
            # row_count is an integer metric -- formatted like the digest
            # (plain "3"/"5"), not the raw stored float ("3.0"/"5.0").
            assert f"{'3':<16}" in text
            assert f"{'5':<16}" in text
            assert "3.0" not in text
            assert "5.0" not in text
            # the check_id hash is dropped from the TUI heading -- noise on
            # a screen already reached by selecting this exact check (the
            # CLI's `dbfresh history` output keeps it; see render_history).
            assert cid not in text

    asyncio.run(scenario())


def _calendar_config(path, db):
    path.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
        "calendar:\n"
        "  timezone: America/New_York\n"
        "checks:\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: row_count\n"
        "    expect: { between: [1, 10] }\n"
    )
    return path


_OFFSET_TIMESTAMP = re.compile(r"T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}")


def test_history_screen_uses_calendar_timezone(tmp_path):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _calendar_config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"
        store = Store(store_path)
        cid = check_id(_row_count_check())
        # Noon UTC is 8:00 AM in New York (EDT), so a friendly History
        # timestamp of "8:00 AM" proves the calendar timezone -- not UTC --
        # was applied.
        observed_at = datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC)
        run_id = store.start_run(started_at=observed_at)
        store.record_observation(
            run_id,
            Result(
                object="t",
                metric="row_count",
                status=Status.OK,
                source="s",
                value=3,
                check_id=cid,
            ),
            observed_at=observed_at,
        )
        store.finish_run(run_id, Status.OK, finished_at=observed_at)
        store.close()

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")  # Home -> ObjectDetailScreen
            await pilot.pause()
            await pilot.press("enter")  # ObjectDetailScreen -> HistoryScreen
            await pilot.pause()

            assert isinstance(app.screen, HistoryScreen)
            text = str(app.screen.query_one("#history-text").content)
            assert "2026-07-08  8:00 AM (Wed)" in text

    asyncio.run(scenario())


def test_report_screen_uses_calendar_timezone(tmp_path, pump_until):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _calendar_config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("r")
            # The Run action starts the check run on a background worker;
            # wait for it to finish rather than a single pause, so the
            # report below always reflects a completed run instead of
            # racing it.
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(pilot, lambda: app.last_run is not None)
            await pilot.press("p")
            await pilot.pause()

            assert isinstance(app.screen, ReportScreen)
            text = str(app.screen.query_one("#report-text").content)
            header = text.splitlines()[0]
            assert _OFFSET_TIMESTAMP.search(header)

    asyncio.run(scenario())


def test_report_action_shows_last_in_session_run_digest(tmp_path, pump_until):
    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("r")
            # The Run action starts the check run on a background worker;
            # wait for it to finish rather than a single pause, so the
            # report below always reflects a completed run instead of
            # racing it.
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(pilot, lambda: app.last_run is not None)
            await pilot.press("p")
            await pilot.pause()

            assert isinstance(app.screen, ReportScreen)
            text = str(app.screen.query_one("#report-text").content)
            assert "DATA CHECK REPORT" in text
            assert "null_rate" in text  # the one failing check is listed

    asyncio.run(scenario())


def test_report_action_before_any_run_shows_placeholder(tmp_path):
    """No in-session run and nothing in the store either (a fresh install)
    -- the placeholder, not a reconstruction attempt."""

    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()

            assert isinstance(app.screen, ReportScreen)
            text = str(app.screen.query_one("#report-text").content)
            assert "no runs recorded yet" in text

    asyncio.run(scenario())


def test_report_action_reconstructs_from_store_when_no_session_run(tmp_path):
    """A restart: no in-session run, but the store has a completed one from
    before -- the Report screen reconstructs its digest from the store
    instead of showing the "no runs" placeholder."""

    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"
        store = Store(store_path)
        run_id = store.start_run()
        store.record_observation(
            run_id,
            Result(
                object="t",
                metric="row_count",
                status=Status.OK,
                source="s",
                value=3,
                check_id=check_id(_row_count_check()),
            ),
        )
        store.record_observation(
            run_id,
            Result(
                object="t",
                metric="null_rate",
                status=Status.FAIL,
                source="s",
                value=0.9,
                expected="max 0.1",
                check_id=check_id(_null_rate_check()),
            ),
        )
        store.finish_run(run_id, Status.FAIL)
        store.close()

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()

            assert isinstance(app.screen, ReportScreen)
            assert app.screen._run is None  # no in-session run triggered this
            text = str(app.screen.query_one("#report-text").content)
            assert "DATA CHECK REPORT" in text
            assert "reconstructed from stored observations" in text
            assert "null_rate" in text  # the one failing check is listed
            assert "max 0.1" in text  # its expectation is still shown

    asyncio.run(scenario())


def test_report_action_prefers_in_session_run_over_store(tmp_path, pump_until):
    """An old completed run sits in the store, but a run happened this
    session too -- the Report screen shows the in-session run (fuller
    detail) rather than falling back to the stale store reconstruction."""

    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"
        store = Store(store_path)
        old_run = store.start_run()
        store.record_observation(
            old_run,
            Result(
                object="t",
                metric="row_count",
                status=Status.OK,
                source="s",
                value=3,
                check_id=check_id(_row_count_check()),
            ),
        )
        store.finish_run(old_run, Status.OK)
        store.close()

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("r")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(pilot, lambda: app.last_run is not None)
            await pilot.press("p")
            await pilot.pause()

            assert isinstance(app.screen, ReportScreen)
            assert app.screen._run is app.last_run
            text = str(app.screen.query_one("#report-text").content)
            assert "null_rate" in text  # the in-session run's failing check
            assert "reconstructed from stored observations" not in text

    asyncio.run(scenario())


def test_status_grids_keep_cell_colors_on_the_cursor_row(tmp_path):
    """Both status grids set cursor_foreground_priority="renderable" --
    the DataTable default ("css") forces every cell on the selected row to
    one flat foreground, which would erase the OK/WARN/FAIL/... encoding
    on exactly the row under focus. See app.tcss's .datatable--cursor rule
    for the matching cursor background restyle."""

    async def scenario():
        db = tmp_path / "data.db"
        _seed_db(db)
        cfg = _config(tmp_path / "config.yaml", db)
        store_path = tmp_path / "obs.db"

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#dashboard-grid", DataTable)
            assert table.cursor_foreground_priority == "renderable"

            await pilot.press("enter")
            await pilot.pause()
            detail_table = app.screen.query_one(DataTable)
            assert detail_table.cursor_foreground_priority == "renderable"

    asyncio.run(scenario())


# -- Home grid view controls: filter / search --------------------------------


def _multi_object_app(tmp_path):
    cfg = _multi_object_config(tmp_path / "config.yaml")
    store_path = tmp_path / "obs.db"
    store = Store(store_path)
    _seed_status(store, "orders", Status.OK)
    _seed_status(store, "items", Status.FAIL)
    # "archive" is left unobserved -- never-observed ("unknown").
    store.close()
    return DbfreshApp(config_path=cfg, store_path=str(store_path))


def test_toggle_non_ok_filter_hides_ok_rows_and_restores_on_second_press(
    tmp_path,
):
    async def scenario():
        app = _multi_object_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#dashboard-grid", DataTable)
            assert table.row_count == 4  # header("s") + archive/items/orders

            await pilot.press("f")
            await pilot.pause()
            # orders is OK -- hidden; items (FAIL) and archive (unobserved)
            # stay, since only exact-OK is dropped -- source "s" still has
            # visible objects, so its header stays too.
            assert set(_row_order(table)) == {
                header_key("s"),
                "s\x1fitems",
                "s\x1farchive",
            }

            await pilot.press("f")
            await pilot.pause()
            assert table.row_count == 4

    asyncio.run(scenario())


def test_search_filters_rows_live_as_text_changes(tmp_path):
    async def scenario():
        app = _multi_object_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#dashboard-grid", DataTable)

            await pilot.press("slash")
            await pilot.pause()
            search = app.query_one("#grid-search")
            assert search.display
            assert app.focused is search

            search.value = "item"
            await pilot.pause()
            assert _row_order(table) == [header_key("s"), "s\x1fitems"]

            search.value = ""
            await pilot.pause()
            assert table.row_count == 4

    asyncio.run(scenario())


def test_search_enter_commits_and_hides_box_keeping_the_filter(tmp_path):
    async def scenario():
        app = _multi_object_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#dashboard-grid", DataTable)

            await pilot.press("slash")
            await pilot.pause()
            search = app.query_one("#grid-search")
            search.value = "item"
            await pilot.pause()

            await pilot.press("enter")
            await pilot.pause()

            assert not search.display
            # The filter is still applied -- only closed, not cancelled.
            assert _row_order(table) == [header_key("s"), "s\x1fitems"]
            assert app.focused is table

    asyncio.run(scenario())


def test_search_escape_clears_the_filter_and_hides_the_box(tmp_path):
    async def scenario():
        app = _multi_object_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#dashboard-grid", DataTable)

            await pilot.press("slash")
            await pilot.pause()
            search = app.query_one("#grid-search")
            search.value = "item"
            await pilot.pause()

            await pilot.press("escape")
            await pilot.pause()

            assert not search.display
            assert search.value == ""
            assert table.row_count == 4
            assert app.focused is table

    asyncio.run(scenario())


def test_escape_before_search_is_opened_does_nothing(tmp_path):
    async def scenario():
        app = _multi_object_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#dashboard-grid", DataTable)
            assert table.row_count == 4

            await pilot.press("escape")
            await pilot.pause()

            assert table.row_count == 4

    asyncio.run(scenario())


def test_view_status_indicator_reflects_active_controls_and_hides_when_inactive(
    tmp_path,
):
    async def scenario():
        app = _multi_object_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            indicator = app.query_one("#view-status", Static)
            assert not indicator.display

            await pilot.press("f")
            await pilot.pause()
            assert indicator.display
            assert "non-OK" in str(indicator.render())

            await pilot.press("f")
            await pilot.pause()
            assert not indicator.display

    asyncio.run(scenario())


def test_search_matching_nothing_shows_no_matching_rows_not_the_empty_state(
    tmp_path,
):
    async def scenario():
        app = _multi_object_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            search = app.query_one("#grid-search")
            search.value = "nonexistent"
            await pilot.pause()

            table = app.query_one("#dashboard-grid", DataTable)
            assert not table.display
            empty_state = app.query_one("#empty-state", Static)
            assert empty_state.display
            text = str(empty_state.render())
            assert "no rows match" in text
            assert "press 'c'" not in text  # not the zero-checks message

    asyncio.run(scenario())


def test_active_view_survives_a_dashboard_refresh(tmp_path):
    """A run (or a reload) rebuilds the grid through refresh_dashboard --
    the active filter/search must still be applied afterward rather than
    silently resetting."""

    async def scenario():
        app = _multi_object_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("f")
            await pilot.pause()
            table = app.query_one("#dashboard-grid", DataTable)
            assert set(_row_order(table)) == {
                header_key("s"),
                "s\x1fitems",
                "s\x1farchive",
            }

            app.refresh_dashboard()
            await pilot.pause()

            assert set(_row_order(table)) == {
                header_key("s"),
                "s\x1fitems",
                "s\x1farchive",
            }

    asyncio.run(scenario())


# -- Home grid grouping: source headers and cursor skip ----------------------


def _two_source_config(path):
    """Two sources, one object each -- "s" -> "orders", "t" -> "items" --
    so a Pilot arrow-key traversal actually crosses a header row sitting
    between two objects (the single-source _multi_object_app above never
    puts a header anywhere but the very top)."""
    path.write_text(
        "sources:\n"
        '  s: { type: sqlite, database: "unused.db" }\n'
        '  t: { type: sqlite, database: "unused.db" }\n'
        "checks:\n"
        "  - source: s\n"
        "    object: orders\n"
        "    metric: row_count\n"
        "    expect: { between: [1, 10] }\n"
        "  - source: t\n"
        "    object: items\n"
        "    metric: row_count\n"
        "    expect: { between: [1, 10] }\n"
    )
    return path


def _two_source_app(tmp_path):
    cfg = _two_source_config(tmp_path / "config.yaml")
    store_path = tmp_path / "obs.db"
    store = Store(store_path)
    _seed_status(store, "orders", Status.OK, source="s")
    _seed_status(store, "items", Status.FAIL, source="t")
    store.close()
    return DbfreshApp(config_path=cfg, store_path=str(store_path))


def test_home_grid_groups_objects_under_a_header_row_per_source(tmp_path):
    async def scenario():
        app = _two_source_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#dashboard-grid", DataTable)
            assert _row_order(table) == [
                header_key("s"),
                "s\x1forders",
                header_key("t"),
                "t\x1fitems",
            ]
            assert table.get_cell("s\x1forders", "label") == "orders"
            assert table.get_cell("t\x1fitems", "label") == "items"

    asyncio.run(scenario())


def test_home_grid_initial_cursor_skips_the_leading_header(tmp_path):
    async def scenario():
        app = _two_source_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#dashboard-grid", DataTable)
            row_key = table.coordinate_to_cell_key(
                table.cursor_coordinate
            ).row_key
            assert row_key.value == "s\x1forders"

    asyncio.run(scenario())


def test_home_grid_cursor_down_skips_a_header_row_between_two_sources(
    tmp_path,
):
    async def scenario():
        app = _two_source_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#dashboard-grid", DataTable)
            # Starts on s.orders (row 1); row 2 is header("t") -- down must
            # skip straight to t.items (row 3), not land on the header.
            await pilot.press("down")
            await pilot.pause()
            row_key = table.coordinate_to_cell_key(
                table.cursor_coordinate
            ).row_key
            assert row_key.value == "t\x1fitems"
            assert not is_header_key(row_key.value)

    asyncio.run(scenario())


def test_home_grid_cursor_up_skips_a_header_row_between_two_sources(tmp_path):
    async def scenario():
        app = _two_source_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#dashboard-grid", DataTable)
            await pilot.press(
                "down"
            )  # s.orders -> t.items (skips header("t"))
            await pilot.pause()
            await pilot.press("up")  # t.items -> s.orders (skips header("t"))
            await pilot.pause()
            row_key = table.coordinate_to_cell_key(
                table.cursor_coordinate
            ).row_key
            assert row_key.value == "s\x1forders"

    asyncio.run(scenario())


def test_home_grid_cursor_up_at_the_first_object_row_does_not_move_to_the_header(
    tmp_path,
):
    async def scenario():
        app = _two_source_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#dashboard-grid", DataTable)
            await pilot.press(
                "up"
            )  # already on s.orders -- header("s") is above
            await pilot.pause()
            row_key = table.coordinate_to_cell_key(
                table.cursor_coordinate
            ).row_key
            assert row_key.value == "s\x1forders"

    asyncio.run(scenario())


def test_toggle_non_ok_filter_drops_a_sources_header_when_all_its_objects_hide(
    tmp_path,
):
    """The 'f' filter narrows rows before populate_grid groups them -- a
    source with nothing left visible contributes no header at all, rather
    than an orphaned header over zero objects."""

    async def scenario():
        app = _two_source_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#dashboard-grid", DataTable)

            await pilot.press("f")
            await pilot.pause()
            # s.orders is OK -- hidden, and with it source "s"'s header;
            # t.items (FAIL) and its header stay.
            assert _row_order(table) == [header_key("t"), "t\x1fitems"]

    asyncio.run(scenario())
