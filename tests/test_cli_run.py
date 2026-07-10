from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.cli import main


def _seed_db(path):
    adapter = SqliteAdapter(str(path))
    adapter.rows("CREATE TABLE t (id INTEGER)")
    adapter.rows("INSERT INTO t (id) VALUES (1), (2), (3)")
    adapter.close()


def _config(path, db, expect):
    path.write_text(
        f'sources:\n  s: {{ type: sqlite, database: "{db}" }}\n'
        "checks:\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: row_count\n"
        f"    expect: {expect}\n"
    )
    return path


def test_run_all_ok_exits_zero(tmp_path, capsys):
    db = tmp_path / "data.db"
    _seed_db(db)
    cfg = _config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
    code = main(["run", "-c", str(cfg)])
    assert code == 0
    assert "1 passed" in capsys.readouterr().out


def test_run_failure_exits_two(tmp_path):
    db = tmp_path / "data.db"
    _seed_db(db)
    cfg = _config(tmp_path / "config.yaml", db, "{ max: 1 }")
    assert main(["run", "-c", str(cfg)]) == 2
