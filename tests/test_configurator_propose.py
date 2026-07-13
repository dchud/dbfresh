"""Metadata-driven proposal bundle: against the real sqlite adapter
for genuine columns/keys, and constructed ObjectInfo for capability-absence
and Databricks-only paths."""

from dbfresh.adapters.base import Category, Column, Dialect, ObjectInfo
from dbfresh.adapters.databricks import DatabricksDialect
from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.configurator import propose_checks


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
