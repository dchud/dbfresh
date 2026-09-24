"""Lineage metadata on a `tables:` entry -- `description`, `tags`,
`upstream`, `downstream` -- annotation the user maintains by hand about
what feeds a table and what reads it. Config only: it is parsed and
validated here, and never persisted to the observation store.
"""

import pytest
from helpers import write_config, write_file

from dbfresh.config import (
    ConfigError,
    LineageRef,
    TableMeta,
    load_config,
    validate_config,
)

_SOURCES = """
sources:
  s: { type: sqlite, database: ":memory:" }
"""


def _messages(path) -> list[str]:
    return [p.message for p in validate_config(path, env={}).problems]


def test_full_metadata_loads_into_config_tables(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
tables:
  - source: s
    object: dbo.fct_sales
    description: Nightly sales fact, one row per line item.
    tags: [tier1, finance]
    upstream:
      - usp_load_fct_sales
      - name: pl_sales_ingest
        kind: pipeline
        url: https://adf.example.com/pl_sales_ingest
    downstream:
      - name: Sales Exec Daily
        kind: dashboard
      - dbo.agg_sales_monthly
    checks:
      - metric: row_count
        expect: { min: 1 }
""",
    )
    config = load_config(path, env={})
    assert config.tables == {
        ("s", "dbo.fct_sales"): TableMeta(
            description="Nightly sales fact, one row per line item.",
            tags=("tier1", "finance"),
            upstream=(
                LineageRef(name="usp_load_fct_sales"),
                LineageRef(
                    name="pl_sales_ingest",
                    kind="pipeline",
                    url="https://adf.example.com/pl_sales_ingest",
                ),
            ),
            downstream=(
                LineageRef(name="Sales Exec Daily", kind="dashboard"),
                LineageRef(name="dbo.agg_sales_monthly"),
            ),
        )
    }
    # Metadata never becomes part of a check.
    assert [c.object for c in config.checks] == ["dbo.fct_sales"]


def test_an_entry_without_metadata_is_not_in_config_tables(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
tables:
  - source: s
    object: t
    checks:
      - metric: row_count
        expect: { min: 1 }
""",
    )
    assert load_config(path, env={}).tables == {}


def test_null_metadata_fields_count_as_unset(tmp_path):
    # `description:` with no value is YAML null -- the entry sets nothing,
    # so it records no metadata and does not claim the table.
    path = write_config(
        tmp_path,
        _SOURCES
        + """
tables:
  - source: s
    object: t
    description:
    checks:
      - metric: row_count
        expect: { min: 1 }
  - source: s
    object: t
    description: The real one.
""",
    )
    config = load_config(path, env={})
    assert config.tables == {
        ("s", "t"): TableMeta(description="The real one.")
    }


def test_a_metadata_only_entry_is_legal(tmp_path):
    # Lineage for a table that has no checks yet.
    path = write_config(
        tmp_path,
        _SOURCES
        + """
tables:
  - source: s
    object: staging_orders
    upstream: [sftp_drop]
""",
    )
    config = load_config(path, env={})
    assert config.checks == []
    assert config.tables == {
        ("s", "staging_orders"): TableMeta(
            upstream=(LineageRef(name="sftp_drop"),)
        )
    }


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("description: 42", "description must be a string"),
        ("tags: finance", "tags must be a list of strings"),
        ("tags: [finance, 7]", "tags must be a list of strings"),
        ("upstream: usp_load", "upstream must be a list"),
        ("downstream: [42]", "downstream item must be a name or a mapping"),
        ("upstream: [{kind: job}]", "is missing 'name'"),
        ("upstream: [{name: a, owner: b}]", "has unknown field(s): ['owner']"),
        ("upstream: [{name: a, kind: 3}]", "kind must be a string"),
        ("downstream: [{name: a, url: [x]}]", "url must be a string"),
    ],
)
def test_malformed_metadata_is_reported(tmp_path, field, expected):
    path = write_config(
        tmp_path,
        _SOURCES
        + f"""
tables:
  - source: s
    object: t
    {field}
    checks:
      - metric: row_count
        expect: {{ min: 1 }}
""",
    )
    messages = _messages(path)
    assert len(messages) == 1
    assert messages[0].startswith("table s.t: ")
    assert expected in messages[0]
    with pytest.raises(ConfigError, match="table s.t: "):
        load_config(path, env={})


def test_validate_collects_every_metadata_problem_at_once(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
tables:
  - source: s
    object: t
    description: 42
    tags: finance
    upstream: [{kind: job}]
""",
    )
    messages = _messages(path)
    assert len(messages) == 3


def test_metadata_keys_are_still_rejected_on_a_flat_check(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
checks:
  - source: s
    object: t
    metric: row_count
    expect: { min: 1 }
    upstream: [usp_load]
""",
    )
    with pytest.raises(ConfigError, match="unknown check field"):
        load_config(path, env={})


def test_metadata_on_two_entries_for_one_table_is_an_error(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
tables:
  - source: s
    object: t
    description: First.
    checks:
      - metric: row_count
        expect: { min: 1 }
  - source: s
    object: t
    tags: [second]
""",
    )
    problems = validate_config(path, env={}).problems
    assert len(problems) == 1
    assert "set on more than one tables: entry" in problems[0].message
    # Both entries are in the same file, which is listed once.
    assert problems[0].files == (path,)
    with pytest.raises(ConfigError, match="more than one tables: entry"):
        load_config(path, env={})


def test_a_second_entry_without_metadata_stays_legal(tmp_path):
    path = write_config(
        tmp_path,
        _SOURCES
        + """
tables:
  - source: s
    object: t
    description: The table.
    checks:
      - metric: row_count
        expect: { min: 1 }
  - source: s
    object: t
    checks:
      - metric: schema
        expect: { unchanged: true }
""",
    )
    config = load_config(path, env={})
    assert config.tables == {("s", "t"): TableMeta(description="The table.")}
    assert len(config.checks) == 2


def test_metadata_on_one_table_in_two_files_names_both(tmp_path):
    included = write_file(
        tmp_path / "more.yaml",
        """
tables:
  - source: s
    object: t
    description: From the included file.
""",
    )
    root = write_config(
        tmp_path,
        _SOURCES
        + """
include: [more.yaml]
tables:
  - source: s
    object: t
    description: From the root.
    checks:
      - metric: row_count
        expect: { min: 1 }
""",
    )
    problems = validate_config(root, env={}).problems
    assert len(problems) == 1
    assert {p.name for p in problems[0].files} == {
        root.name,
        included.name,
    }
    # The first file read -- the root -- keeps the table.
    config = validate_config(root, env={}).config
    assert config.tables[("s", "t")].description == "From the root."


def test_metadata_only_entry_with_an_unknown_source_is_an_error(tmp_path):
    # No check sits under this entry to catch the typo, so the entry
    # itself is checked.
    path = write_config(
        tmp_path,
        _SOURCES
        + """
tables:
  - source: warehuose
    object: t
    description: Typo in the source name.
""",
    )
    messages = _messages(path)
    assert messages == [
        "table warehuose.t references unknown source: 'warehuose'"
    ]
    with pytest.raises(ConfigError, match="unknown source"):
        load_config(path, env={})


def test_unknown_source_with_checks_is_reported_once(tmp_path):
    # The check under the entry already reports the unknown source; the
    # metadata must not add a second copy of the same problem.
    path = write_config(
        tmp_path,
        _SOURCES
        + """
tables:
  - source: warehuose
    object: t
    description: Typo in the source name.
    checks:
      - metric: row_count
        expect: { min: 1 }
""",
    )
    messages = _messages(path)
    assert sum("unknown source" in m for m in messages) == 1
