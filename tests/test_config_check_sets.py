"""check_sets: -- named, parameterized check batteries a table pulls in
via `use:`, optionally overriding parameters with `with:` and dropping
items with `skip:`. Purely additive: a flat `checks:` list, a bare
`tables:` entry, and a set-backed `tables:` entry all coexist, and the
whole mechanism reduces to one thing before `_build_check` ever runs --
a raw check dict indistinguishable from one written by hand.
"""

import pytest
from helpers import write_config, write_file

from dbfresh.checks import check_id, parse_duration
from dbfresh.config import ConfigError, load_config, validate_config

_SOURCES = """
sources:
  s: { type: sqlite, database: ":memory:" }
"""


def test_set_and_flat_equivalent_produce_identical_check_ids(tmp_path):
    # This is what makes factoring a live config under check_sets: safe:
    # the same check_id keeps pointing at the same observation history
    # whether a check came from a set or was written out by hand.
    grouped = write_file(
        tmp_path / "grouped.yaml",
        _SOURCES
        + """
check_sets:
  standard:
    with:
      rows: { between: [10000, 500000] }
      max_lag: 26h
    checks:
      - metric: schema
        expect: { unchanged: true }
      - metric: row_count
        expect: "{{ rows }}"
      - metric: freshness
        column: "{{ ts_column }}"
        expect: { max_lag: "{{ max_lag }}" }

tables:
  - source: s
    object: t
    use: standard
    with: { ts_column: modified_at }
    checks:
      - assert: "amount >= 0"
""",
    )
    flat = write_file(
        tmp_path / "flat.yaml",
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
      - metric: freshness
        column: modified_at
        expect: { max_lag: 26h }
      - assert: "amount >= 0"
""",
    )

    grouped_ids = {check_id(c) for c in load_config(grouped, env={}).checks}
    flat_ids = {check_id(c) for c in load_config(flat, env={}).checks}
    assert len(grouped_ids) == 4
    assert grouped_ids == flat_ids


def test_whole_node_substitution_preserves_type(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
check_sets:
  s1:
    with:
      rows: { between: [1, 5] }
      bounds: [1, 5]
      threshold: 5
      col: ts
    checks:
      - metric: row_count
        expect: "{{ rows }}"
      - metric: avg
        column: amt
        expect: { between: "{{ bounds }}" }
      - metric: sum
        column: amt
        expect: { max: "{{ threshold }}" }
      - metric: freshness
        column: "{{ col }}"
        expect: { max_lag: 24h }

tables:
  - source: s
    object: t
    use: s1
""",
    )
    checks = {c.metric: c for c in load_config(path, env={}).checks}

    # mapping
    assert checks["row_count"].expect.operator == "between"
    assert checks["row_count"].expect.operand == [1, 5]
    # list
    assert checks["avg"].expect.operand == [1, 5]
    # number
    assert checks["sum"].expect.operand == 5
    assert isinstance(checks["sum"].expect.operand, int)
    # string
    assert checks["freshness"].column == "ts"


def test_embedded_interpolation_with_a_scalar_parameter(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
check_sets:
  s1:
    with:
      min_amount: 0
    checks:
      - assert: "amount >= {{ min_amount }}"

tables:
  - source: s
    object: t
    use: s1
""",
    )
    cfg = load_config(path, env={})
    assert cfg.checks[0].assert_ == "amount >= 0"


def test_embedded_mapping_parameter_is_an_error(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
check_sets:
  s1:
    with:
      guard: { min_ratio: 0.5, max_ratio: 2.0, baseline: previous }
    checks:
      - assert: "note: {{ guard }}"

tables:
  - source: s
    object: t
    use: s1
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    message = str(excinfo.value)
    assert "guard" in message
    assert "scalar" in message


def test_embedded_list_parameter_is_an_error(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
check_sets:
  s1:
    with:
      ids: [1, 2, 3]
    checks:
      - assert: "id in {{ ids }}"

tables:
  - source: s
    object: t
    use: s1
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    message = str(excinfo.value)
    assert "ids" in message
    assert "scalar" in message


def test_set_defaults_apply_when_table_supplies_no_with(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
check_sets:
  standard:
    with:
      rows: { between: [10000, 500000] }
    checks:
      - metric: row_count
        expect: "{{ rows }}"

tables:
  - source: s
    object: t
    use: standard
""",
    )
    cfg = load_config(path, env={})
    assert cfg.checks[0].expect.operand == [10000, 500000]


def test_table_with_overrides_one_key_others_fall_through_to_the_default(
    tmp_path,
):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
check_sets:
  standard:
    with:
      rows: { between: [10000, 500000] }
      max_lag: 26h
    checks:
      - metric: row_count
        expect: "{{ rows }}"
      - metric: freshness
        column: modified_at
        expect: { max_lag: "{{ max_lag }}" }

tables:
  - source: s
    object: t
    use: standard
    with: { rows: { between: [1, 500] } }
""",
    )
    checks = {c.metric: c for c in load_config(path, env={}).checks}
    assert checks["row_count"].expect.operand == [1, 500]
    assert parse_duration(
        checks["freshness"].expect.operand
    ) == parse_duration("26h")


def test_table_with_override_replaces_rather_than_deep_merges(tmp_path):
    # A deep merge of {vs_previous: {...}} and {between: [...]} would
    # produce two expectation operators on one check, which
    # parse_expectation rejects -- so a bug here would raise, not just
    # produce a subtly wrong result.
    path = write_config(
        tmp_path,
        _SOURCES
        + """
check_sets:
  standard:
    with:
      rows: { vs_previous: { baseline: previous, min_ratio: 0.8, max_ratio: 1.2 } }
    checks:
      - metric: row_count
        expect: "{{ rows }}"

tables:
  - source: s
    object: t
    use: standard
    with: { rows: { between: [1, 500] } }
""",
    )
    expect = load_config(path, env={}).checks[0].expect
    assert expect.operator == "between"
    assert expect.operand == [1, 500]


def test_missing_required_parameter_names_table_set_and_parameter(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
check_sets:
  standard:
    checks:
      - metric: freshness
        column: "{{ ts_column }}"
        expect: { max_lag: 24h }

tables:
  - source: s
    object: t
    use: standard
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    message = str(excinfo.value)
    assert "table s.t" in message
    assert "standard" in message
    assert "ts_column" in message


def test_with_key_matching_no_placeholder_is_an_error(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
check_sets:
  standard:
    checks:
      - metric: row_count
        expect: { max: 5 }

tables:
  - source: s
    object: t
    use: standard
    with: { bogus: 1 }
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    message = str(excinfo.value)
    assert "table s.t" in message
    assert "standard" in message
    assert "bogus" in message


def test_skip_drops_the_named_metric(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
check_sets:
  standard:
    checks:
      - metric: schema
        expect: { unchanged: true }
      - metric: freshness
        column: modified_at
        expect: { max_lag: 24h }

tables:
  - source: s
    object: t
    use: standard
    skip: [freshness]
""",
    )
    cfg = load_config(path, env={})
    assert [c.metric for c in cfg.checks] == ["schema"]


def test_skip_of_a_metric_the_set_does_not_define_is_an_error(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
check_sets:
  standard:
    checks:
      - metric: schema
        expect: { unchanged: true }

tables:
  - source: s
    object: t
    use: standard
    skip: [row_count]
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    message = str(excinfo.value)
    assert "row_count" in message
    assert "standard" in message


def test_table_skipping_an_item_may_still_pass_that_items_parameter(
    tmp_path,
):
    # The unmatched with:-key check runs against the set's *full*
    # placeholder set, ignoring skip: -- otherwise a set-level default
    # used only by a skipped item would become an error for every table
    # that skips it.
    path = write_config(
        tmp_path,
        _SOURCES
        + """
check_sets:
  standard:
    checks:
      - metric: schema
        expect: { unchanged: true }
      - metric: freshness
        column: "{{ ts_column }}"
        expect: { max_lag: 24h }

tables:
  - source: s
    object: t
    use: standard
    skip: [freshness]
    with: { ts_column: modified_at }
""",
    )
    cfg = load_config(path, env={})
    assert [c.metric for c in cfg.checks] == ["schema"]


def test_expanded_checks_come_before_the_tables_own_inline_checks(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
check_sets:
  standard:
    checks:
      - metric: schema
        expect: { unchanged: true }

tables:
  - source: s
    object: t
    use: standard
    checks:
      - assert: "amount >= 0"
""",
    )
    cfg = load_config(path, env={})
    assert cfg.checks[0].metric == "schema"
    assert cfg.checks[1].assert_ == "amount >= 0"


def test_set_defined_in_included_file_used_by_root_table(tmp_path):
    root = write_file(
        tmp_path / "config.yaml",
        _SOURCES
        + """
include:
  - sets.yaml
tables:
  - source: s
    object: t
    use: standard
""",
    )
    write_file(
        tmp_path / "sets.yaml",
        """
check_sets:
  standard:
    checks:
      - metric: row_count
        expect: { max: 5 }
""",
    )
    cfg = load_config(root, env={})
    assert cfg.checks[0].metric == "row_count"


def test_set_defined_in_root_used_by_table_in_included_file(tmp_path):
    root = write_file(
        tmp_path / "config.yaml",
        _SOURCES
        + """
include:
  - checks.yaml
check_sets:
  standard:
    checks:
      - metric: row_count
        expect: { max: 5 }
checks: []
""",
    )
    write_file(
        tmp_path / "checks.yaml",
        """
tables:
  - source: s
    object: t
    use: standard
""",
    )
    cfg = load_config(root, env={})
    assert cfg.checks[0].metric == "row_count"


def test_check_set_name_defined_in_two_files_is_an_error(tmp_path):
    root = write_file(
        tmp_path / "config.yaml",
        _SOURCES
        + """
include:
  - checks.yaml
check_sets:
  standard:
    checks:
      - metric: row_count
        expect: { max: 5 }
checks: []
""",
    )
    write_file(
        tmp_path / "checks.yaml",
        """
check_sets:
  standard:
    checks:
      - metric: schema
        expect: { unchanged: true }
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(root, env={})
    message = str(excinfo.value)
    assert "standard" in message
    assert "more than one file" in message


def test_unknown_key_on_a_check_set_is_an_error(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
check_sets:
  standard:
    description: "a battery"
    checks:
      - metric: row_count
        expect: { max: 5 }

tables:
  - source: s
    object: t
    use: standard
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    message = str(excinfo.value)
    assert "standard" in message
    assert "description" in message


def test_check_set_item_declaring_its_own_source_is_an_error(tmp_path):
    # Same rule a nested check under tables: follows. A set item is
    # applied to whichever table uses it, so it cannot name a source or
    # object of its own without contradicting the table it expands into.
    path = write_config(
        tmp_path,
        _SOURCES
        + """
check_sets:
  standard:
    checks:
      - source: s
        metric: row_count
        expect: { max: 5 }

tables:
  - source: s
    object: t
    use: standard
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    message = str(excinfo.value)
    assert "standard" in message
    assert "source" in message


def test_use_references_an_unknown_check_set_is_an_error(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
tables:
  - source: s
    object: t
    use: nonexistent
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    message = str(excinfo.value)
    assert "table s.t" in message
    assert "nonexistent" in message


def test_with_or_skip_without_use_is_an_error(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
tables:
  - source: s
    object: t
    with: { foo: 1 }
    checks:
      - metric: row_count
        expect: { max: 5 }
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    message = str(excinfo.value)
    assert "table s.t" in message
    assert "use" in message


def test_validate_collects_several_check_set_problems_with_provenance(
    tmp_path,
):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
check_sets:
  standard:
    checks:
      - metric: freshness
        column: "{{ ts_column }}"
        expect: { max_lag: 24h }

tables:
  - source: s
    object: t
    use: standard
    with: { bogus: 1 }
  - source: s
    object: u
    use: missing_set
""",
    )
    result = validate_config(path, env={})
    messages = [p.message for p in result.problems]
    assert any("bogus" in m and "table s.t" in m for m in messages)
    assert any("ts_column" in m and "table s.t" in m for m in messages)
    assert any("missing_set" in m and "table s.u" in m for m in messages)
