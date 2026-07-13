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
    params = parse_sqlserver_url("sqlserver://user:pass@host/prod?database=mydb")
    assert params == SqlServerConnectionParams(
        server="host\\prod", port=None, database="mydb", user="user", password="pass"
    )


def test_default_port_applied_when_omitted():
    params = parse_sqlserver_url("sqlserver://user:pass@host/mydb")
    assert params.port == 1433


def test_explicit_port_is_respected():
    params = parse_sqlserver_url("sqlserver://user:pass@host:1500/mydb")
    assert params.port == 1500


def test_named_instance_omits_port_even_if_explicit_port_given():
    params = parse_sqlserver_url("sqlserver://user:pass@host:1500/prod?database=mydb")
    assert params.server == "host\\prod"
    assert params.port is None


@pytest.mark.parametrize("scheme", ["sqlserver", "mssql", "ms"])
def test_scheme_aliases_all_accepted(scheme):
    params = parse_sqlserver_url(f"{scheme}://user:pass@host/mydb")
    assert params.database == "mydb"


def test_url_encoded_password_is_decoded():
    params = parse_sqlserver_url("sqlserver://user:p%40ss%2Fw0rd@host/mydb")
    assert params.password == "p@ss/w0rd"


def test_url_encoded_user_is_decoded():
    params = parse_sqlserver_url("sqlserver://dom%5Cuser:pass@host/mydb")
    assert params.user == "dom\\user"


def test_url_encoded_database_path_segment_is_decoded():
    params = parse_sqlserver_url("sqlserver://user:pass@host/my%20db")
    assert params.database == "my db"


def test_url_encoded_database_query_param_is_decoded():
    params = parse_sqlserver_url("sqlserver://user:pass@host/prod?database=my%20db")
    assert params.database == "my db"


def test_missing_database_raises():
    with pytest.raises(ValueError, match="database"):
        parse_sqlserver_url("sqlserver://user:pass@host")


def test_missing_database_with_empty_path_raises():
    with pytest.raises(ValueError, match="database"):
        parse_sqlserver_url("sqlserver://user:pass@host/")


def test_bad_scheme_raises():
    with pytest.raises(ValueError, match="scheme"):
        parse_sqlserver_url("postgresql://user:pass@host/mydb")


def test_missing_credentials_default_to_empty_strings():
    params = parse_sqlserver_url("sqlserver://host/mydb")
    assert params.user == ""
    assert params.password == ""
