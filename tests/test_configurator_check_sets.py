"""check_sets: and configurator's cross-file dedup
(partition_new_checks / _raw_checks_in): a table using a set defined in a
different file must still be seen by the dedup scan, or `dbfresh add`
would re-propose checks that already exist.
"""

from helpers import write_file

from dbfresh.configurator import partition_new_checks

_SOURCES = """
sources:
  s: { type: sqlite, database: ":memory:" }
"""

_PROPOSED = [
    {
        "source": "s",
        "object": "t",
        "metric": "row_count",
        "expect": {"max": 999},
    }
]


def test_dedup_expands_a_table_whose_set_is_defined_in_another_file(
    tmp_path,
):
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

    new, already_defined = partition_new_checks(root, _PROPOSED)
    assert new == []
    assert already_defined == _PROPOSED


def test_dedup_expands_a_table_using_a_set_defined_in_an_included_file(
    tmp_path,
):
    root = write_file(
        tmp_path / "config.yaml",
        _SOURCES
        + """
include:
  - checks.yaml
tables:
  - source: s
    object: t
    use: standard
checks: []
""",
    )
    write_file(
        tmp_path / "checks.yaml",
        """
check_sets:
  standard:
    checks:
      - metric: row_count
        expect: { max: 5 }
""",
    )

    new, already_defined = partition_new_checks(root, _PROPOSED)
    assert new == []
    assert already_defined == _PROPOSED
