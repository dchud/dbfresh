"""`tables:` -- grouping checks that share a source/object so `source:`
and `object:` are stated once instead of repeated on every check under
them. Purely additive: the flat `checks:` list keeps working exactly as
before, and both forms may coexist in one config.
"""

import pytest
from helpers import write_config, write_file

from dbfresh.checks import check_id
from dbfresh.config import ConfigError, load_config, validate_config

_SOURCES = """
sources:
  s: { type: sqlite, database: ":memory:" }
"""


def test_grouped_and_flat_configs_produce_identical_check_ids(tmp_path):
    # check_id hashes only source, object, metric, and the discriminant --
    # never which form (tables: or flat checks:) produced the check. This
    # is the guarantee that lets an existing config be restructured under
    # tables: without orphaning a single stored observation: the same
    # check_id keeps pointing at the same history either way.
    flat = write_file(
        tmp_path / "flat.yaml",
        _SOURCES
        + """
checks:
  - source: s
    object: t
    metric: schema
    expect: { unchanged: true }
  - source: s
    object: t
    metric: row_count
    expect: { between: [10000, 500000] }
  - source: s
    object: t
    assert: "amount >= 0"
""",
    )
    grouped = write_file(
        tmp_path / "grouped.yaml",
        _SOURCES
        + """
tables:
  - source: s
    object: t
    checks:
      - metric: schema
        expect: { unchanged: true }
      - metric: row_count
        expect: { between: [10000, 500000] }
      - assert: "amount >= 0"
""",
    )

    flat_ids = {check_id(c) for c in load_config(flat, env={}).checks}
    grouped_ids = {check_id(c) for c in load_config(grouped, env={}).checks}
    assert len(flat_ids) == 3
    assert flat_ids == grouped_ids


def test_flat_and_grouped_checks_coexist_in_one_config(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
checks:
  - source: s
    object: flat_table
    metric: row_count
    expect: { max: 5 }

tables:
  - source: s
    object: grouped_table
    checks:
      - metric: row_count
        expect: { max: 5 }
      - assert: "1 = 1"
""",
    )
    cfg = load_config(path, env={})
    assert {c.object for c in cfg.checks} == {"flat_table", "grouped_table"}
    assert len(cfg.checks) == 3


def test_included_file_tables_alone(tmp_path):
    root = write_file(
        tmp_path / "config.yaml",
        _SOURCES
        + """
include:
  - checks/*.yaml
checks: []
""",
    )
    write_file(
        tmp_path / "checks" / "a.yaml",
        """
tables:
  - source: s
    object: included_table
    checks:
      - metric: row_count
        expect: { max: 5 }
""",
    )
    cfg = load_config(root, env={})
    assert [c.object for c in cfg.checks] == ["included_table"]


def test_included_file_tables_alongside_checks(tmp_path):
    root = write_file(
        tmp_path / "config.yaml",
        _SOURCES
        + """
include:
  - checks/*.yaml
checks: []
""",
    )
    write_file(
        tmp_path / "checks" / "a.yaml",
        """
checks:
  - source: s
    object: flat_included
    metric: row_count
    expect: { max: 5 }
tables:
  - source: s
    object: grouped_included
    checks:
      - metric: row_count
        expect: { max: 5 }
""",
    )
    cfg = load_config(root, env={})
    assert {c.object for c in cfg.checks} == {
        "flat_included",
        "grouped_included",
    }


def test_nested_check_declaring_own_source_is_an_error_naming_the_table(
    tmp_path,
):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
tables:
  - source: s
    object: t
    checks:
      - source: s
        metric: row_count
        expect: { max: 5 }
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    message = str(excinfo.value)
    assert "table s.t" in message
    assert "source" in message


def test_nested_check_declaring_own_object_is_an_error_naming_the_table(
    tmp_path,
):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
tables:
  - source: s
    object: t
    checks:
      - object: t
        metric: row_count
        expect: { max: 5 }
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    message = str(excinfo.value)
    assert "table s.t" in message
    assert "object" in message


def test_table_entry_missing_source_is_reported_once_naming_the_table(
    tmp_path,
):
    # The entry states source and object for every check under it, so
    # omitting one is a single table-level mistake. Left to _build_check
    # it would surface as "missing required field" once per nested check
    # -- three identical lines here -- none of them naming the table.
    path = write_config(
        tmp_path,
        _SOURCES
        + """
tables:
  - object: t
    checks:
      - metric: row_count
        expect: { max: 5 }
      - metric: schema
        expect: { unchanged: true }
      - assert: "amount >= 0"
""",
    )

    result = validate_config(path, env={})

    assert len(result.problems) == 1
    message = result.problems[0].message
    assert "table ?.t" in message
    assert "source" in message


def test_unknown_key_on_table_entry_is_an_error(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
tables:
  - source: s
    object: t
    tags: [important]
    checks:
      - metric: row_count
        expect: { max: 5 }
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    message = str(excinfo.value)
    assert "table s.t" in message
    assert "tags" in message


def test_unknown_key_still_rejected_on_a_flat_check(tmp_path):
    # The same key ("tags") is rejected on a flat check too, via its own
    # unknown-field validation -- table entries and check blocks validate
    # against separate key sets, but neither silently accepts it.
    path = write_config(
        tmp_path,
        _SOURCES
        + """
checks:
  - source: s
    object: t
    metric: row_count
    tags: [important]
    expect: { max: 5 }
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    message = str(excinfo.value)
    assert "unknown check field" in message
    assert "tags" in message


def test_table_entry_with_no_checks_contributes_nothing(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
tables:
  - source: s
    object: t
checks:
  - source: s
    object: u
    metric: row_count
    expect: { max: 5 }
""",
    )
    cfg = load_config(path, env={})
    assert [c.object for c in cfg.checks] == ["u"]


def test_defaults_merge_into_grouped_checks_same_as_flat(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
defaults:
  severity: warn
tables:
  - source: s
    object: t
    checks:
      - metric: row_count
        expect: { max: 5 }
""",
    )
    cfg = load_config(path, env={})
    assert cfg.checks[0].severity == "warn"


def test_validate_reports_problems_across_both_forms_with_table_provenance(
    tmp_path,
):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
checks:
  - source: s
    object: flat_bad
    metric: row_count
    expect: 5

tables:
  - source: s
    object: grouped_bad
    checks:
      - metric: row_count
""",
    )
    result = validate_config(path, env={})
    messages = [p.message for p in result.problems]
    assert len(messages) == 2
    assert any(
        "invalid expectation" in m and "flat_bad" in m for m in messages
    )
    assert any("no expectation" in m and "grouped_bad" in m for m in messages)


def test_validate_collects_malformed_table_entry_without_aborting(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
tables:
  - source: s
    object: bad_table
    tags: [oops]
    checks:
      - metric: row_count
        expect: { max: 5 }
  - source: s
    object: good_table
    checks:
      - metric: row_count
        expect: { max: 5 }
""",
    )
    result = validate_config(path, env={})
    messages = [p.message for p in result.problems]
    assert len(messages) == 1
    assert "table s.bad_table" in messages[0]
    assert "tags" in messages[0]
    # The malformed entry doesn't block the well-formed one alongside it.
    assert "good_table" in {c.object for c in result.config.checks}
