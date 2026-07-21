"""The `dbfresh ui` subcommand: thin CLI wiring onto DbfreshApp.

The app's own behavior (dashboard, run, configure, history) is exercised
via Textual's Pilot harness in tests/test_tui_*.py; these tests only prove
that the CLI parses `ui`'s flags and constructs the app correctly, without
actually starting an interactive Textual session.
"""

from pathlib import Path

from dbfresh.cli import main


class _FakeApp:
    instances: list[_FakeApp] = []

    def __init__(
        self, config_path, store_path=None, initial_config=None, missing_secrets=None
    ):
        self.config_path = config_path
        self.store_path = store_path
        self.initial_config = initial_config
        self.missing_secrets = missing_secrets
        _FakeApp.instances.append(self)

    def run(self):
        pass


def test_ui_command_constructs_app_with_config_and_store(tmp_path, monkeypatch):
    _FakeApp.instances.clear()
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\nchecks: []\n")
    monkeypatch.setattr("dbfresh.tui.app.DbfreshApp", _FakeApp)

    store = tmp_path / "obs.db"
    code = main(["ui", "-c", str(cfg), "--store", str(store)])

    assert code == 0
    assert len(_FakeApp.instances) == 1
    launched = _FakeApp.instances[0]
    assert str(launched.config_path) == str(cfg)
    assert launched.store_path == str(store)


def test_ui_command_passes_the_already_parsed_config_to_the_app(tmp_path, monkeypatch):
    # _ui_command validates the config before ever constructing the app; it
    # must hand that same Config to DbfreshApp instead of just the path, so
    # on_mount() doesn't parse the same unchanged file a second time.
    _FakeApp.instances.clear()
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\nchecks: []\n")
    monkeypatch.setattr("dbfresh.tui.app.DbfreshApp", _FakeApp)

    code = main(["ui", "-c", str(cfg)])

    assert code == 0
    launched = _FakeApp.instances[0]
    assert launched.initial_config is not None
    assert launched.initial_config.sources == {}


def test_ui_command_defaults_config_path_and_no_store_override(tmp_path, monkeypatch):
    # No -c and no DBFRESH_CONFIG: config discovery finds config.yaml right
    # in the current directory, so the app is launched with its absolute
    # path (a discovered path always is -- see test_cli_config_discovery.py)
    # rather than the bare literal "config.yaml".
    _FakeApp.instances.clear()
    monkeypatch.delenv("DBFRESH_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\nchecks: []\n")
    monkeypatch.setattr("dbfresh.tui.app.DbfreshApp", _FakeApp)

    code = main(["ui"])

    assert code == 0
    launched = _FakeApp.instances[0]
    assert Path(launched.config_path) == cfg.resolve()
    assert launched.store_path is None


def test_ui_command_starts_with_a_missing_config_file(tmp_path, monkeypatch):
    # Unlike a config that exists but fails to load (still a hard error --
    # see test_cli_config_errors.py), a missing file is a brand-new
    # project: `ui` starts against an empty in-memory config instead of
    # refusing to launch, mirroring `dbfresh add`'s own tolerance for a
    # missing config.
    _FakeApp.instances.clear()
    cfg = tmp_path / "does_not_exist.yaml"
    monkeypatch.setattr("dbfresh.tui.app.DbfreshApp", _FakeApp)

    code = main(["ui", "-c", str(cfg)])

    assert code == 0
    launched = _FakeApp.instances[0]
    assert launched.initial_config is not None
    assert launched.initial_config.sources == {}
    assert launched.initial_config.checks == []


def test_ui_command_starts_with_undefined_secret_var(tmp_path, monkeypatch):
    # Unlike run/history/prune/add (still a hard config error -- see
    # test_cli_config_errors.py), an undefined ${VAR} secret does not stop
    # `ui` from launching: the app is constructed and started with the
    # missing name(s) passed through instead.
    _FakeApp.instances.clear()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        'sources:\n  s: { type: sqlite, database: "${DB_PASSWORD}" }\nchecks: []\n'
    )
    monkeypatch.delenv("DB_PASSWORD", raising=False)
    monkeypatch.setattr("dbfresh.tui.app.DbfreshApp", _FakeApp)

    code = main(["ui", "-c", str(cfg)])

    assert code == 0
    launched = _FakeApp.instances[0]
    assert launched.missing_secrets == frozenset({"DB_PASSWORD"})
    # The literal token is left in place rather than resolved, so a check
    # against this source comes back ERROR on a run instead of crashing.
    assert launched.initial_config.sources["s"].params["database"] == "${DB_PASSWORD}"


def test_ui_command_no_missing_secrets_passes_empty_set(tmp_path, monkeypatch):
    _FakeApp.instances.clear()
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\nchecks: []\n")
    monkeypatch.setattr("dbfresh.tui.app.DbfreshApp", _FakeApp)

    code = main(["ui", "-c", str(cfg)])

    assert code == 0
    launched = _FakeApp.instances[0]
    assert launched.missing_secrets == frozenset()


def test_ui_command_non_variable_config_error_still_refuses_to_launch(
    tmp_path, monkeypatch, capsys
):
    # A genuinely broken config (here: an unknown source reference) is not
    # covered by the tolerant undefined-variable path -- it still refuses
    # to launch, exactly like every other config-reading command.
    _FakeApp.instances.clear()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n"
        "  s: { type: sqlite, database: ':memory:' }\n"
        "checks:\n"
        "  - source: other\n"
        "    object: t\n"
        "    metric: row_count\n"
        "    expect: { max: 5 }\n"
    )
    monkeypatch.setattr("dbfresh.tui.app.DbfreshApp", _FakeApp)

    code = main(["ui", "-c", str(cfg)])

    captured = capsys.readouterr()
    assert code == 3
    assert captured.err.strip() == (
        "config error: check references unknown source: 'other'"
    )
    assert _FakeApp.instances == []
