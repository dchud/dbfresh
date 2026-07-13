"""Structured logging: off by default, opt-in via -v/-vv or DBFRESH_LOG_LEVEL.

Every event goes to stderr only -- the digest / ``--json`` report on stdout
must be identical whether or not logging is enabled, so these tests always
check both streams, not just the one they are primarily about.
"""

import json
import logging

import pytest

from dbfresh.cli import main
from dbfresh.logsetup import configure_logging, verbosity_to_level


def _down_config(tmp_path, db):
    """One healthy source, one whose ``type:`` no adapter registers."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'sources:\n  ok: {{ type: sqlite, database: "{db}" }}\n'
        "  down: { type: does_not_exist }\n"
        "checks:\n"
        "  - source: ok\n"
        "    object: t\n"
        "    metric: row_count\n"
        "    expect: { between: [1, 10] }\n"
        "  - source: down\n"
        "    object: whatever\n"
        "    metric: row_count\n"
        "    expect: { max: 5 }\n"
    )
    return cfg


def test_verbosity_to_level_maps_v_count_to_stdlib_level():
    assert verbosity_to_level(0) == logging.WARNING
    assert verbosity_to_level(1) == logging.INFO
    assert verbosity_to_level(2) == logging.DEBUG
    assert verbosity_to_level(5) == logging.DEBUG  # caps at DEBUG


def test_configure_logging_rejects_unknown_env_level():
    with pytest.raises(ValueError, match="DBFRESH_LOG_LEVEL"):
        configure_logging(0, env={"DBFRESH_LOG_LEVEL": "NOT_A_LEVEL"})


def test_quiet_by_default_no_stderr_on_a_healthy_run(
    tmp_path, capsys, seed_row_count_db, row_count_config
):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = row_count_config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")

    code = main(["run", "-c", str(cfg), "--no-store"])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""
    assert "1 passed" in captured.out


def test_verbose_flag_emits_run_start_and_run_end_on_stderr(
    tmp_path, capsys, seed_row_count_db, row_count_config
):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = row_count_config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")

    code = main(["run", "-c", str(cfg), "--no-store", "-v"])

    captured = capsys.readouterr()
    assert code == 0
    assert "event='run_start'" in captured.err
    assert f"config='{cfg}'" in captured.err
    assert "sources=1" in captured.err
    assert "checks=1" in captured.err
    assert "event='run_end'" in captured.err
    assert "status='OK'" in captured.err
    assert "'OK': 1" in captured.err
    assert "elapsed_seconds=" in captured.err
    # stdout is unaffected by turning logging on
    assert "1 passed" in captured.out


def test_verbose_flag_after_subcommand_also_works(
    tmp_path, capsys, seed_row_count_db, row_count_config
):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = row_count_config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")

    code = main(["run", "-c", str(cfg), "--no-store", "-v"])

    assert code == 0
    assert "event='run_start'" in capsys.readouterr().err


def test_source_connect_success_logged_at_info_only_with_verbose(
    tmp_path, capsys, seed_row_count_db, row_count_config
):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = row_count_config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")

    main(["run", "-c", str(cfg), "--no-store"])
    assert capsys.readouterr().err == ""  # quiet by default

    main(["run", "-c", str(cfg), "--no-store", "-v"])
    err = capsys.readouterr().err
    assert "event='source_connect'" in err
    assert "source='s'" in err


def test_unreachable_source_logs_error_event_even_without_verbose(
    tmp_path, capsys, seed_row_count_db
):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = _down_config(tmp_path, db)

    code = main(["run", "-c", str(cfg), "--no-store"])

    captured = capsys.readouterr()
    assert code == 3  # worst status is ERROR
    assert "event='source_connect'" in captured.err
    assert "source='down'" in captured.err
    assert "unknown source type: 'does_not_exist'" in captured.err
    assert "level='error'" in captured.err
    # the healthy source's own (INFO-level) connect success stays suppressed
    assert "source='ok'" not in captured.err


def test_check_error_logged_at_error_without_verbose(
    tmp_path, capsys, seed_row_count_db
):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = _down_config(tmp_path, db)

    main(["run", "-c", str(cfg), "--no-store"])

    err = capsys.readouterr().err
    assert "event='check_error'" in err
    assert "unknown source type: 'does_not_exist'" in err


def test_verbose_debug_emits_per_check_result_events(
    tmp_path, capsys, seed_row_count_db, row_count_config
):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = row_count_config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")

    main(["run", "-c", str(cfg), "--no-store"])
    assert "event='check_result'" not in capsys.readouterr().err  # quiet default

    main(["run", "-c", str(cfg), "--no-store", "-v"])
    assert "event='check_result'" not in capsys.readouterr().err  # info: not yet

    main(["run", "-c", str(cfg), "--no-store", "-vv"])
    err = capsys.readouterr().err
    assert "event='check_result'" in err
    assert "status='OK'" in err


def test_env_var_overrides_verbosity_to_force_debug(
    tmp_path, capsys, monkeypatch, seed_row_count_db, row_count_config
):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = row_count_config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
    monkeypatch.setenv("DBFRESH_LOG_LEVEL", "DEBUG")

    code = main(["run", "-c", str(cfg), "--no-store"])  # no -v at all

    assert code == 0
    assert "event='check_result'" in capsys.readouterr().err


def test_env_var_overrides_verbose_flag_to_suppress_info(
    tmp_path, capsys, monkeypatch, seed_row_count_db, row_count_config
):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = row_count_config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
    monkeypatch.setenv("DBFRESH_LOG_LEVEL", "ERROR")

    code = main(["run", "-c", str(cfg), "--no-store", "-vv"])  # would be DEBUG

    assert code == 0
    assert capsys.readouterr().err == ""


def test_invalid_env_log_level_is_a_clean_error_not_a_traceback(
    tmp_path, capsys, monkeypatch, seed_row_count_db, row_count_config
):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = row_count_config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")
    monkeypatch.setenv("DBFRESH_LOG_LEVEL", "NOT_A_LEVEL")

    code = main(["run", "-c", str(cfg), "--no-store"])

    captured = capsys.readouterr()
    assert code == 3
    assert captured.err.strip() == "error: invalid DBFRESH_LOG_LEVEL: 'NOT_A_LEVEL'"
    assert "Traceback" not in captured.err


def test_json_output_unaffected_by_verbose_logging(
    tmp_path, capsys, seed_row_count_db, row_count_config
):
    db = tmp_path / "data.db"
    seed_row_count_db(db)
    cfg = row_count_config(tmp_path / "config.yaml", db, "{ between: [1, 10] }")

    main(["run", "-c", str(cfg), "--json", "-vv"])
    captured = capsys.readouterr()

    data = json.loads(captured.out)  # stdout is still clean, parseable JSON
    assert data["status"] == "OK"
    assert "event=" not in captured.out
