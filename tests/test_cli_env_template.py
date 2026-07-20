"""`dbfresh env-template`: emit an .env template of a config's ${VAR} refs.

A thin shell over config.collect_referenced_env_vars -- these tests prove
the CLI wiring (argument parsing, stdout format, exit codes, error
reporting), not the collection logic itself, which is unit-tested in
test_config_env_vars.py.
"""

from dbfresh.cli import main

_SOURCES = """
sources:
  s: { type: sqlite, database: "${DB_PATH}" }
"""


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_env_template_prints_sorted_var_lines_and_exits_zero(tmp_path, capsys):
    cfg = _write(
        tmp_path / "config.yaml",
        _SOURCES
        + """
checks:
  - source: s
    object: t
    metric: row_count
    where: "region = '${REGION}'"
    expect: { max: 5 }
""",
    )

    code = main(["env-template", "-c", str(cfg)])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == "DB_PATH=\nREGION=\n"
    assert captured.err == ""


def test_env_template_no_vars_produces_empty_output_and_exits_zero(tmp_path, capsys):
    cfg = _write(
        tmp_path / "config.yaml",
        "sources:\n  s: { type: sqlite, database: ':memory:' }\nchecks: []\n",
    )

    code = main(["env-template", "-c", str(cfg)])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == ""


def test_env_template_missing_config_file_exits_3_with_config_error(tmp_path, capsys):
    missing = tmp_path / "does_not_exist.yaml"

    code = main(["env-template", "-c", str(missing)])

    captured = capsys.readouterr()
    assert code == 3
    assert captured.err.startswith("config error:")
    assert "Traceback" not in captured.err


def test_env_template_output_independent_of_dotenv_file_and_environment(
    tmp_path, capsys, monkeypatch
):
    # env-template is a .env-autoloading command (_CONFIG_READING_COMMANDS),
    # so a .env file beside the config gets loaded into the process
    # environment before dispatch just like `run` does. The template must
    # still list DB_PATH -- collection must not read os.environ, whether
    # the value came from a real env var or from .env.
    cfg = _write(tmp_path / "config.yaml", _SOURCES + "checks: []\n")
    _write(tmp_path / ".env", "DB_PATH=/wherever.db\n")
    monkeypatch.delenv("DB_PATH", raising=False)

    code = main(["env-template", "-c", str(cfg)])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == "DB_PATH=\n"
