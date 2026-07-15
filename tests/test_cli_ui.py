"""The `dbfresh ui` subcommand: thin CLI wiring onto DbfreshApp.

The app's own behavior (dashboard, run, configure, history) is exercised
via Textual's Pilot harness in tests/test_tui_*.py; these tests only prove
that the CLI parses `ui`'s flags and constructs the app correctly, without
actually starting an interactive Textual session.
"""

from dbfresh.cli import main


class _FakeApp:
    instances: list[_FakeApp] = []

    def __init__(self, config_path, store_path=None, initial_config=None):
        self.config_path = config_path
        self.store_path = store_path
        self.initial_config = initial_config
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
    _FakeApp.instances.clear()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("sources: {}\nchecks: []\n")
    monkeypatch.setattr("dbfresh.tui.app.DbfreshApp", _FakeApp)

    code = main(["ui"])

    assert code == 0
    launched = _FakeApp.instances[0]
    assert str(launched.config_path) == "config.yaml"
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
