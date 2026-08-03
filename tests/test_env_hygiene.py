"""committable_env_file: detect a `.env` beside a config that git would track.

Touches real git via subprocess, so every test isolates the git config
(GIT_CONFIG_GLOBAL/GIT_CONFIG_SYSTEM pointed at nonexistent paths) -- a
developer's own global gitignore could otherwise ignore `.env` on some
machines and not others, making these tests depend on machine layout.
"""

from helpers import git_init, isolate_git_config

from dbfresh.env_hygiene import committable_env_file


def test_ungitignored_env_beside_config_is_committable(tmp_path, monkeypatch):
    isolate_git_config(tmp_path, monkeypatch)
    git_init(tmp_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("sources: {}\nchecks: []\n")
    env_path = tmp_path / ".env"
    env_path.write_text("SECRET=x\n")

    assert committable_env_file(config_path) == env_path


def test_gitignored_env_beside_config_is_not_committable(
    tmp_path, monkeypatch
):
    isolate_git_config(tmp_path, monkeypatch)
    git_init(tmp_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("sources: {}\nchecks: []\n")
    (tmp_path / ".env").write_text("SECRET=x\n")
    (tmp_path / ".gitignore").write_text(".env\n")

    assert committable_env_file(config_path) is None


def test_no_env_file_is_not_committable(tmp_path, monkeypatch):
    isolate_git_config(tmp_path, monkeypatch)
    git_init(tmp_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("sources: {}\nchecks: []\n")

    assert committable_env_file(config_path) is None


def test_directory_outside_a_git_working_tree_is_not_committable(
    tmp_path, monkeypatch
):
    isolate_git_config(tmp_path, monkeypatch)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("sources: {}\nchecks: []\n")
    (tmp_path / ".env").write_text("SECRET=x\n")

    assert committable_env_file(config_path) is None
