import pytest

from dbfresh import __version__
from dbfresh.cli import build_parser, main


def test_version_is_set():
    assert __version__


def test_cli_reports_version(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_main_runs():
    assert main([]) == 0
