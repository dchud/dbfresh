"""`dbfresh config validate` and the `config` group's own dispatch.

Thin CLI-level tests over dbfresh.config.validate_config -- the report
format, exit codes, and the `config` group's bare-subcommand behavior
live here; validate_config's own collection and file-attribution logic
is unit-tested in test_config_validate.py.
"""

from helpers import write_file

from dbfresh.cli import main

_SOURCES = """
sources:
  s: { type: sqlite, database: ":memory:" }
"""


def test_config_validate_reports_every_problem_and_exits_3(tmp_path, capsys):
    cfg = write_file(
        tmp_path / "config.yaml",
        _SOURCES
        + """
checks:
  - source: s
    object: t
    metric: row_count
    expect: 5
  - source: s
    object: u
    metric: row_count
    colum: x
    expect: { max: 5 }
""",
    )

    code = main(["config", "validate", "-c", str(cfg)])

    captured = capsys.readouterr()
    assert code == 3
    assert "2 problems found in 1 file" in captured.out
    assert "invalid expectation" in captured.out
    assert "unknown check field" in captured.out
    assert captured.err == ""


def test_config_validate_clean_config_prints_one_line_and_exits_0(
    tmp_path, capsys
):
    cfg = write_file(
        tmp_path / "config.yaml",
        _SOURCES
        + """
checks:
  - source: s
    object: t
    metric: row_count
    expect: { max: 5 }
""",
    )

    code = main(["config", "validate", "-c", str(cfg)])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip() == f"{cfg}: no problems found"
    assert captured.err == ""


def test_config_validate_missing_config_file_exits_3(tmp_path, capsys):
    missing = tmp_path / "does_not_exist.yaml"

    code = main(["config", "validate", "-c", str(missing)])

    captured = capsys.readouterr()
    assert code == 3
    assert captured.err.startswith("config error:")
    assert "Traceback" not in captured.err


def test_config_validate_lists_a_same_file_duplicate_once(tmp_path, capsys):
    # A duplicate check_id names two checks. When both live in the same
    # file -- the ordinary case -- that is one file, and the problem is
    # one bullet under it, not the same bullet twice.
    write_file(
        tmp_path / "config.yaml",
        _SOURCES
        + """
checks:
  - source: s
    object: t
    metric: row_count
    expect: { max: 5 }
  - source: s
    object: t
    metric: row_count
    expect: { max: 9 }
""",
    )

    assert main(["config", "validate", "-c", str(tmp_path / "config.yaml")])

    out = capsys.readouterr().out
    assert out.count("- duplicate check_id") == 1
    assert "1 problem found in 1 file" in out
    assert "listed under each" not in out


def test_config_validate_names_the_file_a_duplicate_check_id_came_from(
    tmp_path, capsys
):
    write_file(
        tmp_path / "config.yaml",
        _SOURCES
        + """
include:
  - checks/*.yaml
checks:
  - source: s
    object: t
    metric: row_count
    id: dup
    expect: { max: 5 }
""",
    )
    write_file(
        tmp_path / "checks" / "a.yaml",
        """
checks:
  - source: s
    object: u
    metric: row_count
    id: dup
    expect: { max: 5 }
""",
    )

    code = main(["config", "validate", "-c", str(tmp_path / "config.yaml")])

    captured = capsys.readouterr()
    assert code == 3
    assert "config.yaml" in captured.out
    assert "checks/a.yaml" in captured.out
    assert "duplicate check_id" in captured.out


def test_config_validate_explains_a_problem_listed_under_two_files(
    tmp_path, capsys
):
    # The one problem spanning two files is listed under both, so the
    # bullets outnumber the total in the header. Unexplained, that reads
    # as an arithmetic error.
    write_file(
        tmp_path / "config.yaml",
        _SOURCES
        + """
include:
  - checks/*.yaml
checks:
  - source: s
    object: t
    metric: row_count
    id: dup
    expect: { max: 5 }
""",
    )
    write_file(
        tmp_path / "checks" / "a.yaml",
        """
checks:
  - source: s
    object: u
    metric: row_count
    id: dup
    expect: { max: 5 }
""",
    )

    assert main(["config", "validate", "-c", str(tmp_path / "config.yaml")])

    out = capsys.readouterr().out
    assert "1 problem found in 2 files" in out
    assert "listed under each" in out
    assert out.count("- duplicate check_id") == 2


def test_config_bare_prints_group_help_and_exits_0(capsys):
    code = main(["config"])

    captured = capsys.readouterr()
    assert code == 0
    assert "validate" in captured.out
    assert "Traceback" not in captured.out
    assert captured.err == ""


def test_config_is_a_config_reading_command_for_discovery(
    tmp_path, capsys, monkeypatch
):
    # -c omitted: config discovery must find config.yaml in cwd, proving
    # "config" is wired into _CONFIG_READING_COMMANDS the same as every
    # other config-reading command.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DBFRESH_CONFIG", raising=False)
    write_file(
        tmp_path / "config.yaml",
        _SOURCES
        + """
checks:
  - source: s
    object: t
    metric: row_count
    expect: { max: 5 }
""",
    )

    code = main(["config", "validate"])

    captured = capsys.readouterr()
    assert code == 0
    assert "no problems found" in captured.out
