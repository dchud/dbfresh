"""Contract-level tests for the native databricks-sql-connector adapter.

No live Databricks SQL warehouse is required: ``scalar``/``rows``/``describe``
are exercised against a fake DB-API connection returning recorded rows,
including ``DESCRIBE DETAIL``/``DESCRIBE HISTORY`` output. A live round-trip
sits behind ``DBFRESH_DATABRICKS_HOST`` (plus ``_HTTP_PATH``/``_TOKEN``) and
is skipped unless those env vars are set.
"""

from __future__ import annotations

import os
import sys
import types
from datetime import datetime

import pytest

from dbfresh.adapters.base import Category, validate_freshness_source
from dbfresh.adapters.databricks import (
    DatabricksAdapter,
    DatabricksDialect,
    category_for_databricks,
)
from dbfresh.adapters.sqlserver import TSqlDialect

DATABRICKS_HOST = os.environ.get("DBFRESH_DATABRICKS_HOST")
DATABRICKS_HTTP_PATH = os.environ.get("DBFRESH_DATABRICKS_HTTP_PATH")
DATABRICKS_TOKEN = os.environ.get("DBFRESH_DATABRICKS_TOKEN")
DATABRICKS_CLIENT_ID = os.environ.get("DBFRESH_DATABRICKS_CLIENT_ID")
DATABRICKS_CLIENT_SECRET = os.environ.get("DBFRESH_DATABRICKS_CLIENT_SECRET")


def test_module_imports_without_databricks_sql_connector_installed():
    # databricks-sql-connector is an optional extra, not a core dependency;
    # this test environment doesn't install it, so a bare import proves the
    # module has no module-level `import databricks.sql`. Constructing an
    # adapter still needs the driver -- see the test below.
    import dbfresh.adapters.databricks as _  # noqa: F401


def test_constructing_adapter_needs_databricks_sql_connector_only_at_connect_time():
    with pytest.raises(ModuleNotFoundError):
        DatabricksAdapter(
            host="x", http_path="/sql/1.0/warehouses/abc", token="t"
        )


class _FakeSqlConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _install_fake_databricks_sql(monkeypatch) -> list[dict]:
    """Fake ``databricks.sql``: ``connect(**kwargs)`` records its kwargs and
    returns a stub connection, so ``DatabricksAdapter.__init__`` can run
    without the real ``databricks-sql-connector`` installed.
    """
    calls: list[dict] = []

    def connect(**kwargs):
        calls.append(kwargs)
        return _FakeSqlConnection()

    package = types.ModuleType("databricks")
    module = types.ModuleType("databricks.sql")
    module.connect = connect
    monkeypatch.setitem(sys.modules, "databricks", package)
    monkeypatch.setitem(sys.modules, "databricks.sql", module)
    return calls


class _FakeConfig:
    """Stands in for ``databricks.sdk.core.Config``: records its kwargs."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _install_fake_databricks_sdk_core(monkeypatch) -> tuple[list, object]:
    """Fake ``databricks.sdk.core``: ``Config`` records the kwargs it's built
    with; ``oauth_service_principal`` records the ``Config`` it receives and
    returns a sentinel, so the credentials-provider path can run without the
    real ``databricks-sdk`` installed.
    """
    calls: list = []
    sentinel = object()

    def oauth_service_principal(config):
        calls.append(config)
        return sentinel

    package = types.ModuleType("databricks.sdk")
    module = types.ModuleType("databricks.sdk.core")
    module.Config = _FakeConfig
    module.oauth_service_principal = oauth_service_principal
    monkeypatch.setitem(sys.modules, "databricks.sdk", package)
    monkeypatch.setitem(sys.modules, "databricks.sdk.core", module)
    return calls, sentinel


def test_pat_auth_calls_connect_with_access_token(monkeypatch):
    connect_calls = _install_fake_databricks_sql(monkeypatch)
    DatabricksAdapter(host="h", http_path="p", token="dapi-xyz")
    assert connect_calls == [
        {
            "server_hostname": "h",
            "http_path": "p",
            "access_token": "dapi-xyz",
        }
    ]


def test_oauth_m2m_auth_calls_connect_with_credentials_provider(monkeypatch):
    connect_calls = _install_fake_databricks_sql(monkeypatch)
    sp_calls, sentinel = _install_fake_databricks_sdk_core(monkeypatch)
    DatabricksAdapter(
        host="h",
        http_path="p",
        client_id="cid",
        client_secret="csec",
        auth_type="oauth_m2m",
    )
    assert len(connect_calls) == 1
    kwargs = connect_calls[0]
    assert kwargs["server_hostname"] == "h"
    assert kwargs["http_path"] == "p"
    assert "access_token" not in kwargs
    provider = kwargs["credentials_provider"]
    assert callable(provider)
    assert sp_calls == []  # not called yet -- connect() only stores it

    result = provider()

    assert result is sentinel
    assert len(sp_calls) == 1
    assert sp_calls[0].kwargs == {
        "host": "https://h",
        "client_id": "cid",
        "client_secret": "csec",
    }


def test_dialect_freshness_capabilities():
    caps = DatabricksDialect().freshness_sources
    assert caps == frozenset({"column", "describe_history", "describe_detail"})


def test_dialect_introspection_capability_is_stats_only():
    # describe() never populates keys (Unity Catalog exposes no constraints
    # here); only last_modified (a "stats" field).
    assert DatabricksDialect().introspection_capabilities == frozenset(
        {"stats"}
    )


def test_dialect_uses_inherited_limit_n():
    sql = DatabricksDialect().limit("SELECT * FROM t", 20)
    assert sql == "SELECT * FROM t LIMIT 20"


@pytest.mark.parametrize(
    ("type_name", "expected"),
    [
        ("int", Category.NUMERIC),
        ("bigint", Category.NUMERIC),
        ("double", Category.NUMERIC),
        ("decimal(18,2)", Category.NUMERIC),
        ("date", Category.TEMPORAL),
        ("timestamp", Category.TEMPORAL),
        ("timestamp_ntz", Category.TEMPORAL),
        ("string", Category.STRING),
        ("varchar(50)", Category.STRING),
        ("boolean", Category.BOOLEAN),
        ("binary", Category.OTHER),
        ("array<string>", Category.OTHER),
        ("map<string,int>", Category.OTHER),
        ("variant", Category.OTHER),
        ("geography", Category.OTHER),
    ],
)
def test_category_for_databricks(type_name, expected):
    assert category_for_databricks(type_name) == expected


class _FakeCursor:
    def __init__(self, connection):
        self._connection = connection
        self.description = None
        self._rows = []

    def execute(self, sql, parameters=None):
        self._connection.queries.append(sql)
        self._connection.parameters_by_query.append(parameters)
        self.description, self._rows = self._connection.resolve(sql)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def fetchmany(self, size):
        rows, self._rows = self._rows[:size], self._rows[size:]
        return rows

    def close(self):
        pass


class _FakeConnection:
    """Maps a SQL substring to a canned ``(description, rows)`` response.

    ``description`` follows PEP 249 shape: a sequence of tuples whose first
    element is the column name; only that first element is used here.
    """

    def __init__(self, responses):
        self._responses = responses
        self.queries: list[str] = []
        self.parameters_by_query: list[dict | None] = []
        self.closed = False

    def parameters_for(self, substring: str) -> dict | None:
        """The bound parameters for the query whose text contains ``substring``."""
        idx = next(i for i, q in enumerate(self.queries) if substring in q)
        return self.parameters_by_query[idx]

    def resolve(self, sql):
        for substring, description, rows in self._responses:
            if substring in sql:
                return description, rows
        raise AssertionError(f"no fake response configured for SQL: {sql!r}")

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        self.closed = True


class _RaisingOnDescribeDetailConnection(_FakeConnection):
    """A fake connection that fails hard if DESCRIBE DETAIL is ever issued.

    Used to prove a code path never queries DESCRIBE DETAIL at all -- a
    plain "no fake response configured" AssertionError from the base fake
    would be ambiguous with a test author simply forgetting to configure
    one.
    """

    def resolve(self, sql):
        if "DESCRIBE DETAIL" in sql:
            raise AssertionError(
                f"DESCRIBE DETAIL must not be issued here, got: {sql!r}"
            )
        return super().resolve(sql)


class _FailingDescribeDetailConnection(_FakeConnection):
    """A fake connection where DESCRIBE DETAIL raises, as it does against a
    table whose format DESCRIBE DETAIL doesn't apply to."""

    def resolve(self, sql):
        if "DESCRIBE DETAIL" in sql:
            raise RuntimeError(
                "DESCRIBE DETAIL is not supported for this table"
            )
        return super().resolve(sql)


def _make_bare_adapter(
    responses, connection_cls=_FakeConnection
) -> DatabricksAdapter:
    """A DatabricksAdapter over a fake connection, no live driver needed."""
    adapter = DatabricksAdapter.__new__(DatabricksAdapter)
    adapter._conn = connection_cls(responses)
    adapter.dialect = DatabricksDialect()
    return adapter


def _desc(*names):
    return [(name,) for name in names]


def test_scalar_returns_first_column_of_first_row():
    adapter = _make_bare_adapter(
        [("SELECT COUNT(*)", _desc("count"), [(42,)])]
    )
    assert adapter.scalar("SELECT COUNT(*) FROM t") == 42


def test_scalar_returns_none_when_no_rows():
    adapter = _make_bare_adapter([("SELECT MAX", _desc("max"), [])])
    assert adapter.scalar("SELECT MAX(x) FROM t") is None


def test_rows_returns_list_of_dicts_using_cursor_description():
    adapter = _make_bare_adapter(
        [("SELECT * FROM t", _desc("a", "b"), [(1, "x"), (2, "y")])]
    )
    assert adapter.rows("SELECT * FROM t") == [
        {"a": 1, "b": "x"},
        {"a": 2, "b": "y"},
    ]


def test_rows_returns_empty_list_when_no_rows():
    adapter = _make_bare_adapter([("SELECT * FROM t", _desc("a"), [])])
    assert adapter.rows("SELECT * FROM t") == []


def test_rows_limited_caps_the_fetch_via_the_cursor():
    adapter = _make_bare_adapter(
        [("SELECT * FROM t", _desc("a"), [(1,), (2,), (3,), (4,), (5,)])]
    )
    assert adapter.rows_limited("SELECT * FROM t", 3) == [
        {"a": 1},
        {"a": 2},
        {"a": 3},
    ]


def test_rows_limited_returns_every_row_when_under_the_cap():
    adapter = _make_bare_adapter(
        [("SELECT * FROM t", _desc("a"), [(1,), (2,)])]
    )
    assert adapter.rows_limited("SELECT * FROM t", 5) == [{"a": 1}, {"a": 2}]


def test_rows_limited_returns_empty_list_when_no_rows():
    adapter = _make_bare_adapter([("SELECT * FROM t", _desc("a"), [])])
    assert adapter.rows_limited("SELECT * FROM t", 5) == []


def test_close_closes_the_underlying_connection():
    adapter = _make_bare_adapter([])
    adapter.close()
    assert adapter._conn.closed is True


_COLUMNS_DESC = _desc("column_name", "data_type", "is_nullable")
_TABLES_DESC = _desc("table_type")
_NOT_A_VIEW = ("information_schema.tables", _TABLES_DESC, [("MANAGED",)])


def test_describe_populates_columns_from_information_schema():
    adapter = _make_bare_adapter(
        [
            (
                "information_schema.columns",
                _COLUMNS_DESC,
                [
                    ("id", "int", "NO"),
                    ("amount", "decimal(18,2)", "YES"),
                    ("seen_at", "timestamp", "YES"),
                ],
            ),
            _NOT_A_VIEW,
            ("DESCRIBE DETAIL", _desc("lastModified"), []),
        ]
    )
    info = adapter.describe("main.gold.customer_360")
    by_name = {c.name: c for c in info.columns}
    assert by_name["id"].category == Category.NUMERIC
    assert by_name["id"].nullable is False
    assert by_name["amount"].category == Category.NUMERIC
    assert by_name["amount"].type == "decimal(18,2)"
    assert by_name["seen_at"].category == Category.TEMPORAL
    assert by_name["seen_at"].nullable is True


def test_describe_queries_the_catalog_qualified_information_schema():
    adapter = _make_bare_adapter(
        [
            ("information_schema.columns", _COLUMNS_DESC, []),
            _NOT_A_VIEW,
            ("DESCRIBE DETAIL", _desc("lastModified"), []),
        ]
    )
    adapter.describe("main.gold.customer_360")
    query = next(
        q for q in adapter._conn.queries if "information_schema.columns" in q
    )
    assert "main.information_schema.columns" in query
    assert "table_schema = :table_schema" in query
    assert "table_name = :table_name" in query
    assert adapter._conn.parameters_for("information_schema.columns") == {
        "table_schema": "gold",
        "table_name": "customer_360",
    }


def test_describe_columns_query_binds_a_name_containing_a_quote():
    # A single quote in a user-typed object name (the wizard/TUI feed
    # exactly this path) must never land inside the SQL text -- bound
    # parameters, not string interpolation, keep the query well-formed.
    adapter = _make_bare_adapter(
        [("information_schema.columns", _COLUMNS_DESC, [])]
    )
    adapter._columns("o'brien")
    query = adapter._conn.queries[-1]
    assert "o'brien" not in query
    assert adapter._conn.parameters_by_query[-1] == {"table_name": "o'brien"}


def test_describe_keys_is_always_none():
    adapter = _make_bare_adapter(
        [
            ("information_schema.columns", _COLUMNS_DESC, []),
            _NOT_A_VIEW,
            ("DESCRIBE DETAIL", _desc("lastModified"), []),
        ]
    )
    info = adapter.describe("main.gold.customer_360")
    assert info.keys is None


def test_describe_last_modified_from_describe_detail():
    when = datetime(2026, 1, 5, 8, 30)
    adapter = _make_bare_adapter(
        [
            ("information_schema.columns", _COLUMNS_DESC, []),
            _NOT_A_VIEW,
            ("DESCRIBE DETAIL", _desc("lastModified"), [(when,)]),
        ]
    )
    info = adapter.describe("main.gold.customer_360")
    assert info.last_modified == when


def test_describe_last_modified_none_when_describe_detail_returns_no_rows():
    adapter = _make_bare_adapter(
        [
            ("information_schema.columns", _COLUMNS_DESC, []),
            _NOT_A_VIEW,
            ("DESCRIBE DETAIL", _desc("lastModified"), []),
        ]
    )
    info = adapter.describe("main.gold.customer_360")
    assert info.last_modified is None


def test_describe_is_view_true_when_table_type_is_view():
    adapter = _make_bare_adapter(
        [
            ("information_schema.columns", _COLUMNS_DESC, []),
            ("information_schema.tables", _TABLES_DESC, [("VIEW",)]),
            ("DESCRIBE DETAIL", _desc("lastModified"), []),
        ]
    )
    info = adapter.describe("main.gold.active_customers")
    assert info.is_view is True


def test_describe_is_view_false_for_a_base_table():
    adapter = _make_bare_adapter(
        [
            ("information_schema.columns", _COLUMNS_DESC, []),
            _NOT_A_VIEW,
            ("DESCRIBE DETAIL", _desc("lastModified"), []),
        ]
    )
    info = adapter.describe("main.gold.customer_360")
    assert info.is_view is False


def test_describe_is_view_false_when_catalog_has_no_matching_row():
    adapter = _make_bare_adapter(
        [
            ("information_schema.columns", _COLUMNS_DESC, []),
            ("information_schema.tables", _TABLES_DESC, []),
            ("DESCRIBE DETAIL", _desc("lastModified"), []),
        ]
    )
    info = adapter.describe("main.gold.missing_object")
    assert info.is_view is False


def test_describe_queries_the_catalog_qualified_information_schema_tables():
    adapter = _make_bare_adapter(
        [
            ("information_schema.columns", _COLUMNS_DESC, []),
            _NOT_A_VIEW,
            ("DESCRIBE DETAIL", _desc("lastModified"), []),
        ]
    )
    adapter.describe("main.gold.customer_360")
    query = next(
        q for q in adapter._conn.queries if "information_schema.tables" in q
    )
    assert "main.information_schema.tables" in query
    assert "table_schema = :table_schema" in query
    assert "table_name = :table_name" in query
    assert adapter._conn.parameters_for("information_schema.tables") == {
        "table_schema": "gold",
        "table_name": "customer_360",
    }


def test_describe_is_view_query_binds_a_name_containing_a_quote():
    adapter = _make_bare_adapter(
        [("information_schema.tables", _TABLES_DESC, [])]
    )
    adapter._is_view("o'brien")
    query = adapter._conn.queries[-1]
    assert "o'brien" not in query
    assert adapter._conn.parameters_by_query[-1] == {"table_name": "o'brien"}


def test_describe_never_issues_describe_detail_for_a_view():
    # DESCRIBE DETAIL is valid only for Delta tables; issuing it against a
    # view raises. describe() must compute is_view first and skip DESCRIBE
    # DETAIL entirely for a view -- proven here by a connection that fails
    # hard if DESCRIBE DETAIL is ever queried.
    adapter = _make_bare_adapter(
        [
            ("information_schema.columns", _COLUMNS_DESC, []),
            ("information_schema.tables", _TABLES_DESC, [("VIEW",)]),
        ],
        connection_cls=_RaisingOnDescribeDetailConnection,
    )
    info = adapter.describe("main.gold.active_customers")
    assert info.is_view is True
    assert info.last_modified is None


def test_describe_last_modified_none_when_describe_detail_raises():
    # A base table whose format DESCRIBE DETAIL can't handle must still
    # describe() successfully, degrading to last_modified=None rather than
    # raising the whole describe() call.
    adapter = _make_bare_adapter(
        [
            ("information_schema.columns", _COLUMNS_DESC, []),
            _NOT_A_VIEW,
        ],
        connection_cls=_FailingDescribeDetailConnection,
    )
    info = adapter.describe("main.gold.non_delta_table")
    assert info.is_view is False
    assert info.last_modified is None


def test_real_adapter_describe_is_view_true_rejects_describe_history_source():
    # The run-time guard (validate_freshness_source) reads ObjectInfo.is_view;
    # this exercises the real DatabricksAdapter.describe() populating it from
    # a view's catalog entry, not a hand-rolled fake, so it proves the guard
    # actually fires against what the native adapter reports.
    adapter = _make_bare_adapter(
        [
            ("information_schema.columns", _COLUMNS_DESC, []),
            ("information_schema.tables", _TABLES_DESC, [("VIEW",)]),
            ("DESCRIBE DETAIL", _desc("lastModified"), []),
        ]
    )
    info = adapter.describe("main.gold.active_customers")
    with pytest.raises(ValueError, match="view"):
        validate_freshness_source(
            "describe_history", adapter.dialect, info.is_view
        )


def test_describe_history_last_modified_filters_to_data_operations_and_takes_max():
    old_optimize = datetime(2026, 1, 1)
    write = datetime(2026, 1, 3)
    merge = datetime(2026, 1, 4)
    adapter = _make_bare_adapter(
        [
            (
                "DESCRIBE HISTORY",
                _desc("timestamp", "operation"),
                [
                    (merge, "MERGE"),
                    (old_optimize, "OPTIMIZE"),
                    (write, "WRITE"),
                ],
            )
        ]
    )
    assert (
        adapter.describe_history_last_modified("main.gold.customer_360")
        == merge
    )


def test_describe_history_query_is_bounded_by_a_limit():
    # History retention can be long; bound the fetch since only the most
    # recent data operations matter for a max-timestamp scan.
    adapter = _make_bare_adapter(
        [("DESCRIBE HISTORY", _desc("timestamp", "operation"), [])]
    )
    adapter.describe_history_last_modified("main.gold.customer_360")
    query = next(q for q in adapter._conn.queries if "DESCRIBE HISTORY" in q)
    assert "LIMIT" in query


def test_describe_history_last_modified_none_when_no_data_operations():
    adapter = _make_bare_adapter(
        [
            (
                "DESCRIBE HISTORY",
                _desc("timestamp", "operation"),
                [
                    (datetime(2026, 1, 1), "OPTIMIZE"),
                    (datetime(2026, 1, 2), "VACUUM"),
                ],
            )
        ]
    )
    assert (
        adapter.describe_history_last_modified("main.gold.customer_360")
        is None
    )


def test_describe_history_counts_non_write_data_operations():
    # A table rebuilt nightly by CREATE OR REPLACE TABLE AS SELECT (not a
    # WRITE) is a data change; only maintenance operations are excluded.
    ctas = datetime(2026, 1, 5)
    adapter = _make_bare_adapter(
        [
            (
                "DESCRIBE HISTORY",
                _desc("timestamp", "operation"),
                [
                    (datetime(2026, 1, 6), "OPTIMIZE"),
                    (ctas, "CREATE OR REPLACE TABLE AS SELECT"),
                ],
            )
        ]
    )
    assert (
        adapter.describe_history_last_modified("main.gold.customer_360")
        == ctas
    )


def test_validate_freshness_source_accepts_describe_forms_on_a_table():
    validate_freshness_source(
        "describe_history", DatabricksDialect(), is_view=False
    )
    validate_freshness_source(
        "describe_detail", DatabricksDialect(), is_view=False
    )


def test_validate_freshness_source_accepts_column_always():
    validate_freshness_source("column", DatabricksDialect(), is_view=True)
    validate_freshness_source("column", TSqlDialect(), is_view=False)


@pytest.mark.parametrize("source", ["describe_history", "describe_detail"])
def test_validate_freshness_source_rejects_describe_forms_on_a_view(source):
    with pytest.raises(ValueError, match="view"):
        validate_freshness_source(source, DatabricksDialect(), is_view=True)


@pytest.mark.parametrize("source", ["describe_history", "describe_detail"])
def test_validate_freshness_source_rejects_capability_lacking_engine(source):
    with pytest.raises(ValueError, match="tsql"):
        validate_freshness_source(source, TSqlDialect(), is_view=False)


@pytest.mark.skipif(
    not (DATABRICKS_HOST and DATABRICKS_HTTP_PATH and DATABRICKS_TOKEN),
    reason=(
        "set DBFRESH_DATABRICKS_HOST/_HTTP_PATH/_TOKEN to run against a "
        "live Databricks SQL warehouse"
    ),
)
def test_live_describe_reflects_columns_and_last_modified():
    adapter = DatabricksAdapter(
        host=DATABRICKS_HOST,
        http_path=DATABRICKS_HTTP_PATH,
        token=DATABRICKS_TOKEN,
    )
    try:
        info = adapter.describe("main.default.dbfresh_probe")
        assert info.columns
        assert info.keys is None
    finally:
        adapter.close()


@pytest.mark.skipif(
    not (
        DATABRICKS_HOST
        and DATABRICKS_HTTP_PATH
        and DATABRICKS_CLIENT_ID
        and DATABRICKS_CLIENT_SECRET
    ),
    reason=(
        "set DBFRESH_DATABRICKS_HOST/_HTTP_PATH/_CLIENT_ID/_CLIENT_SECRET "
        "to run against a live Databricks SQL warehouse via a service "
        "principal"
    ),
)
def test_live_describe_via_service_principal():
    adapter = DatabricksAdapter(
        host=DATABRICKS_HOST,
        http_path=DATABRICKS_HTTP_PATH,
        client_id=DATABRICKS_CLIENT_ID,
        client_secret=DATABRICKS_CLIENT_SECRET,
        auth_type="oauth_m2m",
    )
    try:
        info = adapter.describe("main.default.dbfresh_probe")
        assert info.columns
        assert info.keys is None
    finally:
        adapter.close()
