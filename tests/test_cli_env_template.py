"""`dbfresh env-template`: emit an .env template of a config's ${VAR} refs.

A thin shell over config.collect_referenced_env_vars -- these tests prove
the CLI wiring (argument parsing, stdout format, exit codes, error
reporting), not the collection logic itself, which is unit-tested in
test_config_env_vars.py.
"""

import subprocess

from dbfresh.cli import main

_SOURCES = """
sources:
  s: { type: sqlite, database: "${DB_PATH}" }
"""


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _isolate_git_config(tmp_path, monkeypatch):
    # committable_env_file runs real git -- pin its config away from the
    # developer's own, so a global gitignore that happens to ignore .env
    # can't make these tests pass or fail depending on machine layout.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "no-system"))


def _git_init(repo_dir):
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)


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


def test_env_template_no_vars_produces_empty_output_and_exits_zero(
    tmp_path, capsys
):
    cfg = _write(
        tmp_path / "config.yaml",
        "sources:\n  s: { type: sqlite, database: ':memory:' }\nchecks: []\n",
    )

    code = main(["env-template", "-c", str(cfg)])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == ""


def test_env_template_missing_config_file_exits_3_with_config_error(
    tmp_path, capsys
):
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


def test_env_template_warns_on_stderr_when_env_file_is_not_gitignored(
    tmp_path, capsys, monkeypatch
):
    _isolate_git_config(tmp_path, monkeypatch)
    monkeypatch.delenv("DBFRESH_CONFIG", raising=False)
    _git_init(tmp_path)
    cfg = _write(tmp_path / "config.yaml", _SOURCES + "checks: []\n")
    env_path = _write(tmp_path / ".env", "DB_PATH=/wherever.db\n")

    code = main(["env-template", "-c", str(cfg)])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == "DB_PATH=\n"
    assert captured.err.startswith("warning:")
    assert str(env_path) in captured.err
    assert "not gitignored" in captured.err


def test_env_template_no_warning_when_env_file_is_gitignored(
    tmp_path, capsys, monkeypatch
):
    _isolate_git_config(tmp_path, monkeypatch)
    monkeypatch.delenv("DBFRESH_CONFIG", raising=False)
    _git_init(tmp_path)
    cfg = _write(tmp_path / "config.yaml", _SOURCES + "checks: []\n")
    _write(tmp_path / ".env", "DB_PATH=/wherever.db\n")
    _write(tmp_path / ".gitignore", ".env\n")

    code = main(["env-template", "-c", str(cfg)])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == "DB_PATH=\n"
    assert captured.err == ""
