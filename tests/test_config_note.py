"""`note:` -- optional freeform context on a check, carrying no structure
and no validation beyond "is a string." Never merged from `defaults:`
(unlike `severity`/`calendar`/`where`/`allow_empty`/`skip_off_schedule`):
a note belongs to the specific check that carries it. Applies uniformly
to a flat check, a check nested under `tables:`, and a check expanded
from a `check_sets:` item, since all three reduce to the same raw dict
before `_build_check` ever runs.
"""

import pytest
from helpers import write_config

from dbfresh.config import ConfigError, load_config, validate_config

_SOURCES = """
sources:
  s: { type: sqlite, database: ":memory:" }
"""


def test_note_on_a_flat_check(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
checks:
  - source: s
    object: t
    metric: row_count
    expect: { between: [10000, 500000] }
    note: dips legitimately on month-end close
""",
    )
    cfg = load_config(path, env={})
    assert cfg.checks[0].note == "dips legitimately on month-end close"


def test_note_on_a_grouped_check(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
tables:
  - source: s
    object: t
    checks:
      - metric: row_count
        expect: { between: [10000, 500000] }
        note: widened after the 2026-06 backfill
""",
    )
    cfg = load_config(path, env={})
    assert cfg.checks[0].note == "widened after the 2026-06 backfill"


def test_note_on_a_set_expanded_check(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
check_sets:
  standard:
    checks:
      - metric: row_count
        expect: { between: [10000, 500000] }
        note: shared across every table pulling in this set

tables:
  - source: s
    object: t
    use: standard
""",
    )
    cfg = load_config(path, env={})
    assert (
        cfg.checks[0].note == "shared across every table pulling in this set"
    )


def test_note_absent_defaults_to_none(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
checks:
  - source: s
    object: t
    metric: row_count
    expect: { max: 5 }
""",
    )
    cfg = load_config(path, env={})
    assert cfg.checks[0].note is None


def test_note_is_not_merged_from_defaults(tmp_path):
    # Unlike severity/calendar/where/allow_empty/skip_off_schedule, a
    # note: in defaults: is simply never read -- it belongs to the check
    # that carries it, not every check that omits its own.
    path = write_config(
        tmp_path,
        _SOURCES
        + """
defaults:
  note: this must never apply to a check that has no note of its own
checks:
  - source: s
    object: t
    metric: row_count
    expect: { max: 5 }
""",
    )
    cfg = load_config(path, env={})
    assert cfg.checks[0].note is None


def test_note_per_table_via_check_set_templating(tmp_path):
    # The documented workaround for a check_sets: note being shared: a
    # {{ note }} placeholder, filled per table from its own with:.
    path = write_config(
        tmp_path,
        _SOURCES
        + """
check_sets:
  standard:
    checks:
      - metric: row_count
        expect: { between: [10000, 500000] }
        note: "{{ note }}"

tables:
  - source: s
    object: t
    use: standard
    with: { note: "table t's own context" }
  - source: s
    object: u
    use: standard
    with: { note: "table u's own context" }
""",
    )
    cfg = load_config(path, env={})
    notes = {c.object: c.note for c in cfg.checks}
    assert notes == {
        "t": "table t's own context",
        "u": "table u's own context",
    }


def test_non_string_note_is_a_validation_error_naming_the_check(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
checks:
  - source: s
    object: t
    metric: row_count
    expect: { max: 5 }
    note: [not, a, string]
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    message = str(excinfo.value)
    assert "note" in message
    assert "s.t" in message


def test_non_string_note_is_collected_alongside_other_problems(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
checks:
  - source: s
    object: t
    metric: row_count
    expect: { max: 5 }
    note: 12345
  - source: s
    object: u
    metric: row_count
    expect: 5
""",
    )
    result = validate_config(path, env={})
    messages = [p.message for p in result.problems]
    assert len(messages) == 2
    assert any("note" in m and "s.t" in m for m in messages)
    assert any("invalid expectation" in m and "s.u" in m for m in messages)
