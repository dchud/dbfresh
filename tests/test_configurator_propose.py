"""Metadata-driven proposal bundle: against the real sqlite adapter
for genuine columns/keys, and constructed ObjectInfo for capability-absence
and Databricks-only paths."""

from dbfresh.adapters.base import Category, Column, Dialect, ObjectInfo
from dbfresh.adapters.databricks import DatabricksDialect
from dbfresh.adapters.sqlite import SqliteAdapter, SqliteDialect
from dbfresh.adapters.sqlserver import TSqlDialect
from dbfresh.configurator import (
    key_introspection_note,
    offered_column_checks,
    propose_checks,
)


def _col(name, category=Category.NUMERIC, nullable=False, type_="INT"):
    return Column(name=name, type=type_, nullable=nullable, category=category)


def _sqlite_table():
    a = SqliteAdapter()
    a.rows(
        "CREATE TABLE fct ("
        "  id INTEGER PRIMARY KEY,"
        "  amount REAL,"
        "  email TEXT,"
        "  modified_at TIMESTAMP"
        ")"
    )
    return a


def test_full_bundle_against_real_sqlite_adapter():
    a = _sqlite_table()
    info = a.describe("fct")
    proposals = propose_checks("s", "fct", info, a.dialect)

    assert {
        "source": "s",
        "object": "fct",
        "metric": "schema",
        "expect": {"unchanged": True},
    } in proposals

    row_count = next(p for p in proposals if p["metric"] == "row_count")
    guards = row_count["expect"]["vs_previous"]
    assert guards["baseline"] == "previous"
    assert guards["min_ratio"] == 0.5
    assert guards["max_ratio"] == 2.0

    freshness = next(p for p in proposals if p["metric"] == "freshness")
    assert freshness["column"] == "modified_at"
    assert freshness["freshness_source"] == "column"

    dup = next(p for p in proposals if p["metric"] == "duplicate_count")
    assert dup["key"] == "id"
    assert dup["expect"] == {"max": 0}
    a.close()


def test_row_count_uses_last_same_weekday_baseline_when_calendar_configured():
    a = _sqlite_table()
    info = a.describe("fct")
    proposals = propose_checks("s", "fct", info, a.dialect, has_calendar=True)
    row_count = next(p for p in proposals if p["metric"] == "row_count")
    assert row_count["expect"]["vs_previous"]["baseline"] == "last_same_weekday"
    a.close()


def test_no_duplicate_count_proposal_without_keys():
    a = SqliteAdapter()
    a.rows("CREATE TABLE t (a INTEGER, b TEXT)")
    info = a.describe("t")
    proposals = propose_checks("s", "t", info, a.dialect)
    assert not [p for p in proposals if p["metric"] == "duplicate_count"]
    a.close()


def test_key_introspection_note_is_none_when_keys_are_present():
    info = ObjectInfo(columns=[_col("id")], keys=[["id"]])
    assert key_introspection_note(Dialect(), info) is None


def test_key_introspection_note_is_none_when_engine_can_say_and_has_none():
    # SqliteDialect/TSqlDialect declare "keys" as an introspection
    # capability, so an object with none is a genuine fact, not a gap.
    info = ObjectInfo(columns=[_col("id")], keys=None)
    assert key_introspection_note(SqliteDialect(), info) is None
    assert key_introspection_note(TSqlDialect(), info) is None


def test_key_introspection_note_explains_when_engine_cannot_say():
    # DatabricksDialect never declares "keys": Unity Catalog exposes no
    # constraint metadata, so a missing duplicate_count proposal here means
    # "this engine cannot say", not "this object has none".
    info = ObjectInfo(columns=[_col("id")], keys=None)
    note = key_introspection_note(DatabricksDialect(), info)
    assert note is not None
    assert "databricks" in note
    assert "duplicate_count" in note


def test_composite_keys_are_skipped():
    info = ObjectInfo(
        columns=[
            Column(name="a", type="INTEGER", nullable=False, category=Category.NUMERIC),
            Column(name="b", type="INTEGER", nullable=False, category=Category.NUMERIC),
        ],
        keys=[["a", "b"]],
    )
    proposals = propose_checks("s", "t", info, Dialect())
    assert not [p for p in proposals if p["metric"] == "duplicate_count"]


def test_each_single_column_key_gets_its_own_duplicate_count_proposal():
    info = ObjectInfo(
        columns=[
            _col("id"),
            _col("email", category=Category.STRING, nullable=True, type_="TEXT"),
        ],
        keys=[["id"], ["email"]],
    )
    proposals = propose_checks("s", "t", info, Dialect())
    dup_keys = {p["key"] for p in proposals if p["metric"] == "duplicate_count"}
    assert dup_keys == {"id", "email"}


def test_ambiguous_timestamp_yields_no_freshness_proposal():
    info = ObjectInfo(
        columns=[
            _col("created_at", category=Category.TEMPORAL, nullable=True),
            _col("updated_at", category=Category.TEMPORAL, nullable=True),
        ]
    )
    proposals = propose_checks("s", "t", info, Dialect())
    assert not [p for p in proposals if p["metric"] == "freshness"]


def test_timestamp_override_forces_freshness_column_when_ambiguous():
    # Front ends detect ambiguity via pick_timestamp_column, ask the user,
    # then hand the choice back here rather than propose_checks guessing.
    info = ObjectInfo(
        columns=[
            _col("created_at", category=Category.TEMPORAL, nullable=True),
            _col("updated_at", category=Category.TEMPORAL, nullable=True),
        ]
    )
    proposals = propose_checks(
        "s", "t", info, Dialect(), timestamp_override="updated_at"
    )
    freshness = next(p for p in proposals if p["metric"] == "freshness")
    assert freshness["column"] == "updated_at"
    assert freshness["freshness_source"] == "column"


def test_databricks_table_with_no_candidate_falls_back_to_describe_history():
    info = ObjectInfo(columns=[_col("id")])
    proposals = propose_checks("s", "t", info, DatabricksDialect())
    freshness = next(p for p in proposals if p["metric"] == "freshness")
    assert freshness["freshness_source"] == "describe_history"
    assert "column" not in freshness


def test_view_with_no_candidate_gets_no_freshness_proposal_even_on_databricks():
    info = ObjectInfo(columns=[_col("id")])
    proposals = propose_checks("s", "t", info, DatabricksDialect(), is_view=True)
    assert not [p for p in proposals if p["metric"] == "freshness"]


def test_no_candidate_on_non_databricks_engine_gets_no_freshness_proposal():
    info = ObjectInfo(columns=[_col("id")])
    proposals = propose_checks("s", "t", info, Dialect())
    assert not [p for p in proposals if p["metric"] == "freshness"]


def test_schema_and_row_count_are_always_proposed_even_with_minimal_metadata():
    info = ObjectInfo(columns=[_col("id")])
    proposals = propose_checks("s", "t", info, Dialect())
    metrics = {p["metric"] for p in proposals}
    assert {"schema", "row_count"} <= metrics


def test_offered_checks_exclude_what_the_propose_flow_already_covers():
    # ``fct``'s proposal already covers freshness on modified_at (the
    # auto-detected timestamp column) and duplicate_count keyed on id (a
    # numeric single-column primary key) -- offered_column_checks, fed that
    # same bundle, must not offer either metric again for those columns.
    a = _sqlite_table()
    info = a.describe("fct")
    proposals = propose_checks("s", "fct", info, a.dialect)

    offers = {
        o["column"]: o["checks"] for o in offered_column_checks(info.columns, proposals)
    }
    assert "freshness" not in offers["modified_at"]
    assert "duplicate_count" not in offers["id"]
    # Neither column's other, non-overlapping offers were affected.
    assert "null_rate" in offers["modified_at"]
    assert "sum" in offers["id"]
    a.close()
