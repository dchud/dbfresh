"""Config discovery: -c PATH > DBFRESH_CONFIG > nearest config.yaml walking
up from the current directory > config.yaml in the current directory.

The unit tests below exercise ``resolve_config_path`` and
``_discover_config`` directly; the CLI integration tests prove the
resolved path reaches a command handler through ``main()``, including the
``.env`` autoload that must use the discovered config's directory.
"""

import os
from pathlib import Path

from dbfresh.cli import _discover_config, main, resolve_config_path

_MINIMAL_CONFIG = "sources: {}\nchecks: []\n"


def _write_config(path: Path, text: str = _MINIMAL_CONFIG) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# ---------------------------------------------------------------------------
# resolve_config_path
# ---------------------------------------------------------------------------


def test_resolve_config_path_explicit_cli_wins_over_env_and_discovery(
    tmp_path,
):
    sub = tmp_path / "sub"
    sub.mkdir()
    _write_config(tmp_path / "config.yaml")  # discoverable, but must lose

    result = resolve_config_path(
        "explicit.yaml", str(tmp_path / "env-config.yaml"), sub
    )

    assert result == Path("explicit.yaml")


def test_resolve_config_path_env_wins_when_no_cli_flag(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    _write_config(tmp_path / "config.yaml")  # discoverable, but must lose
    env_path = str(tmp_path / "env.yaml")

    result = resolve_config_path(None, env_path, sub)

    assert result == Path(env_path)


def test_resolve_config_path_discovers_config_in_a_parent_directory(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    cfg = _write_config(tmp_path / "config.yaml")

    result = resolve_config_path(None, None, sub)

    assert result == cfg.resolve()


def test_resolve_config_path_falls_back_to_bare_filename_when_nothing_found(
    tmp_path, monkeypatch
):
    # Bound discovery to the temp tree (no ``.git`` anywhere in it, home
    # pinned at its root) so the walk-up terminates deterministically
    # instead of depending on the real machine's directory layout.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()

    result = resolve_config_path(None, None, sub)

    assert result == Path("config.yaml")


# ---------------------------------------------------------------------------
# _discover_config / _discovery_boundary
# ---------------------------------------------------------------------------


def test_discover_config_finds_an_ancestor_config_in_a_git_less_tree(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = _write_config(tmp_path / "config.yaml")
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)

    assert _discover_config(sub) == cfg.resolve()


def test_discover_config_does_not_cross_a_git_root(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write_config(
        tmp_path / "config.yaml"
    )  # above the git root; must not be found
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "sub"
    sub.mkdir()

    assert _discover_config(sub) is None


def test_discover_config_finds_a_config_exactly_at_the_git_root(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    cfg = _write_config(repo / "config.yaml")
    sub = repo / "sub"
    sub.mkdir()

    assert _discover_config(sub) == cfg.resolve()


def test_discover_config_returns_none_when_nothing_found_up_to_the_boundary(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)

    assert _discover_config(sub) is None


# ---------------------------------------------------------------------------
# CLI integration -- through main()
# ---------------------------------------------------------------------------


def test_env_template_discovers_config_from_a_subdirectory(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.delenv("DBFRESH_CONFIG", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write_config(
        tmp_path / "config.yaml",
        'sources:\n  s: { type: sqlite, database: "${DB_PATH}" }\nchecks: []\n',
    )
    sub = tmp_path / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)

    code = main(["env-template"])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == "DB_PATH=\n"


def test_env_template_uses_dbfresh_config_env_var(
    tmp_path, capsys, monkeypatch
):
    cfg = _write_config(
        tmp_path / "elsewhere" / "config.yaml",
        'sources:\n  s: { type: sqlite, database: "${DB_PATH}" }\nchecks: []\n',
    )
    elsewhere_cwd = tmp_path / "not-related"
    elsewhere_cwd.mkdir()
    monkeypatch.chdir(elsewhere_cwd)
    monkeypatch.setenv("DBFRESH_CONFIG", str(cfg))

    code = main(["env-template"])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == "DB_PATH=\n"


def test_env_template_explicit_flag_overrides_env_and_discovery(
    tmp_path, capsys, monkeypatch
):
    _write_config(tmp_path / "config.yaml")  # discoverable, must lose
    env_cfg = _write_config(
        tmp_path / "env-config.yaml"
    )  # DBFRESH_CONFIG, must lose
    explicit_cfg = _write_config(
        tmp_path / "explicit.yaml",
        'sources:\n  s: { type: sqlite, database: "${DB_PATH}" }\nchecks: []\n',
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DBFRESH_CONFIG", str(env_cfg))

    code = main(["env-template", "-c", str(explicit_cfg)])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == "DB_PATH=\n"


def test_env_template_dbfresh_config_pointing_at_missing_file_reports_that_path(
    tmp_path, capsys, monkeypatch
):
    missing = tmp_path / "does_not_exist.yaml"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DBFRESH_CONFIG", str(missing))

    code = main(["env-template"])

    captured = capsys.readouterr()
    assert code == 3
    assert str(missing) in captured.err


def test_dotenv_beside_a_discovered_config_is_loaded(tmp_path, monkeypatch):
    monkeypatch.delenv("DBFRESH_CONFIG", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("DBFRESH_DISCOVERY_PROBE", raising=False)
    parent = tmp_path / "parent"
    _write_config(parent / "config.yaml")
    (parent / ".env").write_text("DBFRESH_DISCOVERY_PROBE=loaded\n")
    sub = parent / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)

    code = main(["env-template"])

    assert code == 0
    assert os.environ["DBFRESH_DISCOVERY_PROBE"] == "loaded"
