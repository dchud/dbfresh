from datetime import UTC, datetime, timedelta

from dbfresh.cli import main
from dbfresh.engine import Result, Status
from dbfresh.store import Store


def _config(path, extra=""):
    path.write_text(f"sources: {{}}\nchecks: []\n{extra}")
    return path


def _result(**overrides) -> Result:
    fields = {
        "object": "dbo.fct_sales",
        "metric": "row_count",
        "status": Status.OK,
        "source": "warehouse",
        "value": 1,
        "check_id": "abc123def456",
    }
    fields.update(overrides)
    return Result(**fields)


def test_prune_removes_observations_older_than_configured_retain_days(
    tmp_path, capsys, seed_observations
):
    cfg = _config(tmp_path / "config.yaml", "store: { retain_days: 30 }\n")
    store_path = tmp_path / "obs.db"
    now = datetime.now(UTC)
    seed_observations(
        store_path,
        [
            (_result(check_id="old"), now - timedelta(days=60)),
            (_result(check_id="new"), now - timedelta(days=1)),
        ],
    )
    code = main(["prune", "-c", str(cfg), "--store", str(store_path)])
    assert code == 0
    assert "1" in capsys.readouterr().out

    store = Store(store_path)
    remaining = {
        row["check_id"]
        for row in store._conn.execute(
            "SELECT check_id FROM observation"
        ).fetchall()
    }
    assert remaining == {"new"}
    store.close()


def test_prune_defaults_to_400_days_retention(tmp_path, seed_observations):
    cfg = _config(tmp_path / "config.yaml")
    store_path = tmp_path / "obs.db"
    now = datetime.now(UTC)
    seed_observations(
        store_path,
        [
            (_result(check_id="ancient"), now - timedelta(days=500)),
            (_result(check_id="recent"), now - timedelta(days=100)),
        ],
    )
    main(["prune", "-c", str(cfg), "--store", str(store_path)])

    store = Store(store_path)
    remaining = {
        row["check_id"]
        for row in store._conn.execute(
            "SELECT check_id FROM observation"
        ).fetchall()
    }
    assert remaining == {"recent"}
    store.close()


def test_prune_works_without_config_file(tmp_path, seed_observations):
    store_path = tmp_path / "obs.db"
    now = datetime.now(UTC)
    seed_observations(
        store_path, [(_result(check_id="ancient"), now - timedelta(days=500))]
    )
    code = main(
        [
            "prune",
            "-c",
            str(tmp_path / "nonexistent.yaml"),
            "--store",
            str(store_path),
        ]
    )
    assert code == 0
    store = Store(store_path)
    remaining = store._conn.execute(
        "SELECT check_id FROM observation"
    ).fetchall()
    assert remaining == []
    store.close()
