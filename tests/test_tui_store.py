"""Pilot tests for the Store screen: size/count/retention display and the
two-press-confirm prune, reached from Home via the "s" binding."""

import asyncio
from datetime import UTC, datetime, timedelta

from textual.widgets import Button, Static

from dbfresh.models import Result, Status
from dbfresh.store import Store, format_bytes
from dbfresh.tui.app import DbfreshApp
from dbfresh.tui.screens import StoreScreen

_RETAIN_DAYS = 30


def _result(check_id: str, value: float) -> Result:
    return Result(
        object="t",
        metric="row_count",
        status=Status.OK,
        source="s",
        value=value,
        check_id=check_id,
    )


def _config(path, retain_days: int = _RETAIN_DAYS):
    path.write_text(
        f"sources: {{}}\nchecks: []\nstore:\n  retain_days: {retain_days}\n"
    )
    return path


def _seed(
    store: Store, check_id: str, value: float, observed_at: datetime
) -> None:
    run_id = store.start_run(started_at=observed_at)
    store.record_observation(
        run_id, _result(check_id, value), observed_at=observed_at
    )
    store.finish_run(run_id, Status.OK, finished_at=observed_at)


async def _open_store_screen(pilot):
    await pilot.pause()
    await pilot.press("s")
    await pilot.pause()
    assert isinstance(pilot.app.screen, StoreScreen)


def test_store_screen_shows_size_counts_and_retention(tmp_path):
    async def scenario():
        cfg = _config(tmp_path / "config.yaml")
        store_path = tmp_path / "obs.db"
        now = datetime(2026, 7, 10, tzinfo=UTC)
        store = Store(store_path)
        _seed(store, "a", 1, now)
        _seed(store, "b", 2, now)
        store.close()

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await _open_store_screen(pilot)

            info = str(app.screen.query_one("#store-info", Static).render())
            assert str(store_path) in info
            assert "observations: 2" in info
            assert "runs: 2" in info
            assert f"retention: {_RETAIN_DAYS} days" in info
            assert app.store is not None
            assert format_bytes(app.store.size_bytes()) in info

    asyncio.run(scenario())


def test_store_prune_requires_a_second_press(tmp_path):
    async def scenario():
        cfg = _config(tmp_path / "config.yaml")
        store_path = tmp_path / "obs.db"
        now = datetime(2026, 7, 10, tzinfo=UTC)
        store = Store(store_path)
        _seed(store, "old", 1, now - timedelta(days=_RETAIN_DAYS + 10))
        store.close()

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await _open_store_screen(pilot)

            app.screen.query_one("#store-prune-btn", Button).press()
            await pilot.pause()

            # A single press only reveals the confirm row -- nothing is
            # pruned yet.
            assert app.screen.query_one("#store-prune-confirm-row").display
            assert app.store is not None
            assert app.store.observation_count() == 1

    asyncio.run(scenario())


def test_store_prune_cancel_leaves_observations_untouched(tmp_path):
    async def scenario():
        cfg = _config(tmp_path / "config.yaml")
        store_path = tmp_path / "obs.db"
        now = datetime(2026, 7, 10, tzinfo=UTC)
        store = Store(store_path)
        _seed(store, "old", 1, now - timedelta(days=_RETAIN_DAYS + 10))
        store.close()

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await _open_store_screen(pilot)

            app.screen.query_one("#store-prune-btn", Button).press()
            await pilot.pause()
            app.screen.query_one("#store-prune-cancel-btn", Button).press()
            await pilot.pause()

            assert not app.screen.query_one("#store-prune-confirm-row").display
            assert app.store is not None
            assert app.store.observation_count() == 1

    asyncio.run(scenario())


def test_store_prune_confirmed_deletes_old_observations_and_refreshes_counts(
    tmp_path, pump_until
):
    async def scenario():
        cfg = _config(tmp_path / "config.yaml")
        store_path = tmp_path / "obs.db"
        now = datetime(2026, 7, 10, tzinfo=UTC)
        store = Store(store_path)
        _seed(store, "old", 1, now - timedelta(days=_RETAIN_DAYS + 10))
        _seed(store, "new", 2, now - timedelta(days=1))
        store.close()

        app = DbfreshApp(config_path=cfg, store_path=str(store_path))
        async with app.run_test() as pilot:
            await _open_store_screen(pilot)

            app.screen.query_one("#store-prune-btn", Button).press()
            await pilot.pause()
            app.screen.query_one("#store-prune-confirm-btn", Button).press()
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pump_until(
                pilot,
                lambda: (
                    app.store is not None
                    and app.store.observation_count() == 1
                ),
            )

            assert not app.screen.query_one("#store-prune-confirm-row").display
            assert app.store is not None
            # The prune ran on a fresh connection to the same file, not the
            # app's own store connection -- re-querying through the app's
            # store still sees the deletion (same underlying file, WAL
            # makes it visible across connections).
            assert app.store.observation_count() == 1
            remaining = {
                row["check_id"] for row in app.store.history("new")
            } | {row["check_id"] for row in app.store.history("old")}
            assert remaining == {"new"}

            await pump_until(
                pilot,
                lambda: (
                    "pruned"
                    in str(
                        app.screen.query_one(
                            "#store-prune-result", Static
                        ).render()
                    )
                ),
            )

            info = str(app.screen.query_one("#store-info", Static).render())
            assert "observations: 1" in info

            result_text = str(
                app.screen.query_one("#store-prune-result", Static).render()
            )
            assert "pruned 1 observation(s)" in result_text
            assert f"older than {_RETAIN_DAYS} days" in result_text

    asyncio.run(scenario())


def test_pressing_s_on_home_pushes_store_screen(tmp_path):
    async def scenario():
        cfg = tmp_path / "config.yaml"
        cfg.write_text("sources: {}\nchecks: []\n")
        app = DbfreshApp(config_path=cfg, store_path=str(tmp_path / "obs.db"))
        async with app.run_test() as pilot:
            await _open_store_screen(pilot)

    asyncio.run(scenario())
