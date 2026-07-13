"""Every config-reading command reports a validation problem cleanly.

`load_config` raises `ValueError` for a validation problem (unknown source
reference, duplicate check_id, calendar features without a calendar block,
operator misuse, ...); each command must turn that into a single
`config error: <message>` line on stderr and exit 2, rather than letting an
unhandled traceback reach the terminal.
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
    assert code == 2
    assert captured.err.strip() == (
        "config error: check references unknown source: 'other'"
    )
    assert "Traceback" not in captured.err
