"""group_checks_by_table -- the inverse of flatten_table_checks, folding
raw check dicts into tables: entries. Exercised directly here rather than
through either caller (dbfresh config migrate, and
configurator.render_proposal); the CLI-level round trips and behavior
live in test_cli_config_migrate.py and test_cli_add.py, not this
function's own grouping logic.
"""

from dbfresh.config import group_checks_by_table


def test_groups_by_source_and_object_pair():
    checks = [
        {"source": "s", "object": "t", "metric": "row_count"},
        {"source": "s", "object": "u", "metric": "schema"},
        {"source": "s", "object": "t", "metric": "null_rate", "column": "x"},
    ]
    tables = group_checks_by_table(checks)
    assert [(e["source"], e["object"]) for e in tables] == [
        ("s", "t"),
        ("s", "u"),
    ]
    assert [c["metric"] for c in tables[0]["checks"]] == [
        "row_count",
        "null_rate",
    ]


def test_drops_source_and_object_from_nested_checks():
    checks = [{"source": "s", "object": "t", "metric": "row_count"}]
    tables = group_checks_by_table(checks)
    assert tables[0]["checks"] == [{"metric": "row_count"}]


def test_merges_pairs_regardless_of_which_check_contributed_them_first():
    # A table split across two distinct raw-check origins (e.g. one from a
    # flattened tables: entry, one from a flat checks: list) still yields
    # one entry, never two -- this is what makes a partially-migrated file
    # converge instead of duplicating the pair.
    checks = [
        {"source": "s", "object": "t", "metric": "schema"},
        {"source": "s", "object": "u", "metric": "row_count"},
        {"source": "s", "object": "t", "metric": "row_count"},
    ]
    tables = group_checks_by_table(checks)
    assert len(tables) == 2
    t_entry = next(e for e in tables if e["object"] == "t")
    assert [c["metric"] for c in t_entry["checks"]] == ["schema", "row_count"]


def test_empty_input_produces_no_entries():
    assert group_checks_by_table([]) == []
