"""URL parsing for source connection strings (currently: SQL Server)."""

from __future__ import annotations

import pytest

from dbfresh.connection import SqlServerConnectionParams, parse_sqlserver_url


def test_dburl_style_path_segment_is_database():
    # No `?database=` query param: the path segment is the database, per
    # dburl convention.
    params = parse_sqlserver_url("sqlserver://user:pass@host/mydb")
    assert params == SqlServerConnectionParams(
        server="host", port=1433, database="mydb", user="user", password="pass"
    )


def test_native_style_query_database_makes_path_the_instance():
    # `?database=` present: the path segment is a named instance instead,
    # and the port is omitted (SQL Server resolves it dynamically).
    params = parse_sqlserver_url(
        "sqlserver://user:pass@host/prod?database=mydb"
    )
    assert params == SqlServerConnectionParams(
        server="host\\prod",
        port=None,
        database="mydb",
        user="user",
        password="pass",
    )


@pytest.mark.parametrize(
    ("url", "port"),
    [
        pytest.param(
            "sqlserver://user:pass@host/mydb", 1433, id="default-when-omitted"
        ),
        pytest.param(
            "sqlserver://user:pass@host:1500/mydb", 1500, id="explicit"
        ),
    ],
)
def test_port(url, port):
    assert parse_sqlserver_url(url).port == port


def test_named_instance_omits_port_even_if_explicit_port_given():
    params = parse_sqlserver_url(
        "sqlserver://user:pass@host:1500/prod?database=mydb"
    )
    assert params.server == "host\\prod"
    assert params.port is None


@pytest.mark.parametrize("scheme", ["sqlserver", "mssql", "ms"])
def test_scheme_aliases_all_accepted(scheme):
    params = parse_sqlserver_url(f"{scheme}://user:pass@host/mydb")
    assert params.database == "mydb"


@pytest.mark.parametrize(
    ("url", "field", "decoded"),
    [
        pytest.param(
            "sqlserver://user:p%40ss%2Fw0rd@host/mydb",
            "password",
            "p@ss/w0rd",
            id="password",
        ),
        pytest.param(
            "sqlserver://dom%5Cuser:pass@host/mydb",
            "user",
            "dom\\user",
            id="user",
        ),
        pytest.param(
            "sqlserver://user:pass@host/my%20db",
            "database",
            "my db",
            id="database-path-segment",
        ),
        pytest.param(
            "sqlserver://user:pass@host/prod?database=my%20db",
            "database",
            "my db",
            id="database-query-param",
        ),
    ],
)
def test_url_encoded_components_are_decoded(url, field, decoded):
    assert getattr(parse_sqlserver_url(url), field) == decoded


@pytest.mark.parametrize(
    ("url", "message"),
    [
        pytest.param(
            "sqlserver://user:pass@host", "database", id="no-path-at-all"
        ),
        pytest.param(
            "sqlserver://user:pass@host/", "database", id="empty-path"
        ),
        pytest.param(
            "postgresql://user:pass@host/mydb", "scheme", id="bad-scheme"
        ),
    ],
)
def test_unusable_url_raises(url, message):
    with pytest.raises(ValueError, match=message):
        parse_sqlserver_url(url)


def test_missing_credentials_default_to_empty_strings():
    params = parse_sqlserver_url("sqlserver://host/mydb")
    assert params.user == ""
    assert params.password == ""
