from datetime import UTC, datetime

from dbfresh.cli import main
from dbfresh.engine import Result, Status
from dbfresh.store import Store


def _config(path):
    path.write_text("sources: {}\nchecks: []\n")
    return path


def _result(**overrides) -> Result:
    fields = {
        "object": "dbo.fct_sales",
        "metric": "row_count",
        "status": Status.OK,
        "source": "warehouse",
        "value": 100,
        "check_id": "abc123def456",
    }
    fields.update(overrides)
    return Result(**fields)


def _seed(store_path, entries):
    store = Store(store_path)
    run_id = store.start_run()
    for result, observed_at in entries:
        store.record_observation(run_id, result, observed_at=observed_at)
    store.finish_run(run_id, Status.OK)
    store.close()


def test_history_prints_recent_observations(tmp_path, capsys):
    cfg = _config(tmp_path / "config.yaml")
    store_path = tmp_path / "obs.db"
    _seed(
        store_path,
        [
            (_result(value=100), datetime(2026, 7, 8, tzinfo=UTC)),
            (_result(value=120), datetime(2026, 7, 9, tzinfo=UTC)),
        ],
    )
    code = main(
        ["history", "dbo.fct_sales", "-c", str(cfg), "--store", str(store_path)]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "dbo.fct_sales" in out
    assert "100.0" in out
    assert "120.0" in out


def test_history_no_observations_returns_one(tmp_path, capsys):
    cfg = _config(tmp_path / "config.yaml")
    store_path = tmp_path / "obs.db"
    Store(store_path).close()  # empty store, tables exist
    code = main(["history", "dbo.missing", "-c", str(cfg), "--store", str(store_path)])
    assert code == 1
    assert "no observations" in capsys.readouterr().out.lower()


def test_history_ambiguous_object_lists_candidates(tmp_path, capsys):
    cfg = _config(tmp_path / "config.yaml")
    store_path = tmp_path / "obs.db"
    _seed(
        store_path,
        [
            (_result(metric="row_count", check_id="a"), None),
            (_result(metric="null_rate", check_id="b"), None),
        ],
    )
    code = main(
        ["history", "dbo.fct_sales", "-c", str(cfg), "--store", str(store_path)]
    )
    out = capsys.readouterr().out
    assert code == 2
    assert "a" in out
    assert "b" in out


def test_history_source_and_metric_disambiguate(tmp_path, capsys):
    cfg = _config(tmp_path / "config.yaml")
    store_path = tmp_path / "obs.db"
    _seed(
        store_path,
        [
            (_result(metric="row_count", check_id="a"), None),
            (_result(metric="null_rate", check_id="b"), None),
        ],
    )
    code = main(
        [
            "history",
            "dbo.fct_sales",
            "-c",
            str(cfg),
            "--store",
            str(store_path),
            "--metric",
            "row_count",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "row_count" in out


def test_history_limit_flag(tmp_path, capsys):
    cfg = _config(tmp_path / "config.yaml")
    store_path = tmp_path / "obs.db"
    _seed(
        store_path,
        [
            (_result(value=v), datetime(2026, 7, d, tzinfo=UTC))
            for d, v in enumerate([1, 2, 3, 4, 5], start=1)
        ],
    )
    code = main(
        [
            "history",
            "dbo.fct_sales",
            "-c",
            str(cfg),
            "--store",
            str(store_path),
            "-n",
            "2",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "4.0" in out
    assert "5.0" in out
    assert "1.0" not in out


def test_history_works_without_config_file(tmp_path, capsys):
    store_path = tmp_path / "obs.db"
    _seed(store_path, [(_result(value=1), None)])
    code = main(
        [
            "history",
            "dbo.fct_sales",
            "-c",
            str(tmp_path / "nonexistent.yaml"),
            "--store",
            str(store_path),
        ]
    )
    assert code == 0
