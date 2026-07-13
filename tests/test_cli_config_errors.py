"""Every config-reading command reports a load/parse/validation failure cleanly.

`load_config` raises `ConfigError` for every failure mode -- a missing or
unreadable file, a YAML parse error, a missing required field, a bad
expectation, or a validation problem (unknown source reference, duplicate
check_id, calendar features without a calendar block, operator misuse,
...); each command must turn that into a single `config error: <message>`
line on stderr and exit 3 (ERROR), rather than letting an unhandled
traceback reach the terminal. Exit 3 -- not 1 (WARN) or 2 (FAIL) -- because
a config that never loaded is a run that could not complete, not a check
result.
"""

import pytest

from dbfresh.cli import main

_INVALID_CONFIG = (
    "sources:\n"
    "  s: { type: sqlite, database: ':memory:' }\n"
    "checks:\n"
    "  - source: other\n"
    "    object: t\n"
    "    metric: row_count\n"
    "    expect: { max: 5 }\n"
)


def _write_invalid_config(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_INVALID_CONFIG)
    return cfg


@pytest.mark.parametrize(
    "argv_prefix",
    [["run"], ["history", "dbo.fct_sales"], ["prune"], ["add"], ["ui"]],
    ids=["run", "history", "prune", "add", "ui"],
)
def test_command_reports_config_error_cleanly(tmp_path, capsys, argv_prefix):
    cfg = _write_invalid_config(tmp_path)

    code = main([*argv_prefix, "-c", str(cfg)])

    captured = capsys.readouterr()
    assert code == 3
    assert captured.err.strip() == (
        "config error: check references unknown source: 'other'"
    )
    assert "Traceback" not in captured.err


def test_run_missing_config_file_reports_cleanly(tmp_path, capsys):
    missing = tmp_path / "does_not_exist.yaml"

    code = main(["run", "-c", str(missing)])

    captured = capsys.readouterr()
    assert code == 3
    assert captured.err.startswith("config error:")
    assert "Traceback" not in captured.err


def test_run_invalid_yaml_reports_cleanly(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: [this is not: valid: yaml\n")

    code = main(["run", "-c", str(cfg)])

    captured = capsys.readouterr()
    assert code == 3
    assert captured.err.startswith("config error:")
    assert "Traceback" not in captured.err


def test_run_missing_object_field_reports_cleanly(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n"
        "  s: { type: sqlite, database: ':memory:' }\n"
        "checks:\n"
        "  - source: s\n"
        "    metric: row_count\n"
        "    expect: { max: 5 }\n"
    )

    code = main(["run", "-c", str(cfg)])

    captured = capsys.readouterr()
    assert code == 3
    assert captured.err.startswith("config error:")
    assert "Traceback" not in captured.err


def test_run_bad_expectation_reports_cleanly(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n"
        "  s: { type: sqlite, database: ':memory:' }\n"
        "checks:\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: row_count\n"
        "    expect: 5\n"
    )

    code = main(["run", "-c", str(cfg)])

    captured = capsys.readouterr()
    assert code == 3
    assert captured.err.startswith("config error:")
    assert "Traceback" not in captured.err


def test_main_last_resort_guard_reports_unexpected_exception_cleanly(
    tmp_path, capsys, monkeypatch
):
    """No boundary catches this -- main()'s dispatch wrapper is the backstop."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n"
        "  s: { type: sqlite, database: ':memory:' }\n"
        "checks:\n"
        "  - source: s\n"
        "    object: t\n"
        "    metric: row_count\n"
        "    expect: { max: 5 }\n"
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("dbfresh.runner.run_and_persist", _boom)

    code = main(["run", "-c", str(cfg)])

    captured = capsys.readouterr()
    assert code == 3
    assert captured.err.strip() == "error: boom"
    assert "Traceback" not in captured.err
