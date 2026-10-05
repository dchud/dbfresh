"""A misspelled key in the root config, or in its defaults:, store:, or
calendar: section, is a config error naming the key and, when there is an
obvious one, the key it was probably meant to be -- instead of being
ignored while the setting it meant to configure keeps its default."""

from pathlib import Path

import pytest
from helpers import write_config, write_file

from dbfresh.config import ConfigError, load_config, validate_config

_SOURCES = """
sources:
  s: { type: sqlite, database: ":memory:" }
"""


def _messages(path) -> list[str]:
    return [p.message for p in validate_config(path, env={}).problems]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "calender:\n  timezone: UTC\n",
            "top level: unknown key 'calender' -- did you mean 'calendar'?",
        ),
        (
            "defaults:\n  severty: warn\n",
            "defaults: unknown key 'severty' -- did you mean 'severity'?",
        ),
        (
            "store:\n  path: obs.db\n  retian_days: 30\n",
            "store: unknown key 'retian_days' -- did you mean 'retain_days'?",
        ),
        (
            "calendar:\n  timezone: UTC\n  workdyas: [mon]\n",
            "calendar: unknown key 'workdyas' -- did you mean 'workdays'?",
        ),
        (
            "calendar:\n  timezone: UTC\n  holidays:\n    contry: US\n",
            "calendar.holidays: unknown key 'contry' -- did you mean "
            "'country'?",
        ),
    ],
)
def test_a_misspelled_key_is_reported_with_a_suggestion(
    tmp_path, text, expected
):
    path = write_config(tmp_path, _SOURCES + text + "checks: []\n")
    assert _messages(path) == [expected]
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(path, env={})


def test_an_unknown_key_with_no_close_match_has_no_suggestion(tmp_path):
    path = write_config(tmp_path, _SOURCES + "notifications: {}\nchecks: []\n")
    assert _messages(path) == ["top level: unknown key 'notifications'"]


def test_several_unknown_keys_are_collected_at_once(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
incldue: [more.yaml]
defualts:
  severity: warn
calendar:
  timezone: UTC
  workdyas: [mon]
checks: []
""",
    )
    messages = _messages(path)
    assert len(messages) == 3
    assert all("unknown key" in m for m in messages)


def test_every_documented_key_loads_clean(tmp_path):
    write_file(tmp_path / "more.yaml", "checks: []\n")
    path = write_config(
        tmp_path,
        """
version: 1
include: [more.yaml]
store:
  path: obs.db
  retain_days: 30
calendar:
  timezone: UTC
  workdays: [mon, tue, wed, thu, fri]
  holidays:
    country: US
    subdivision: null
    extra: ["2026-11-27"]
    remove: []
sources:
  s: { type: sqlite, database: ":memory:" }
defaults:
  severity: warn
  calendar: business
  where: "1 = 1"
  allow_empty: false
  skip_off_schedule: false
check_sets:
  basic:
    checks:
      - metric: row_count
        expect: { min: 1 }
tables:
  - source: s
    object: t
    use: basic
checks:
  - source: s
    object: u
    metric: row_count
    expect: { min: 1 }
""",
    )
    assert _messages(path) == []


def test_store_as_a_bare_path_is_still_accepted(tmp_path):
    path = write_config(tmp_path, _SOURCES + "store: obs.db\nchecks: []\n")
    assert _messages(path) == []


def test_example_config_has_no_unknown_keys():
    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    messages = _messages(example)
    assert not [m for m in messages if "unknown key" in m]


def test_an_undefined_variable_is_still_reported_first(tmp_path):
    path = write_config(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: "${DBFRESH_TEST_UNSET_DB}" }
calender:
  timezone: UTC
checks: []
""",
    )
    with pytest.raises(ConfigError, match="undefined environment variable"):
        load_config(path, env={})
