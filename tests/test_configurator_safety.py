"""Configurator safety and degradation (§11.3): connection test, existence
check, and unverified-manual-entry degradation."""

from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.configurator import check_object_exists, probe_connection


def test_probe_connection_succeeds_for_a_good_sqlite_source():
    result = probe_connection("sqlite", {"database": ":memory:"})
    assert result.ok is True
    assert result.error is None


def test_probe_connection_fails_for_unknown_source_type():
    result = probe_connection("mystery", {})
    assert result.ok is False
    assert result.error is not None


def test_check_object_exists_true_for_real_table():
    a = SqliteAdapter()
    a.rows("CREATE TABLE t (id INTEGER)")
    result = check_object_exists(a, "t")
    assert result.verified is True
    assert result.exists is True
    assert result.info is not None
    a.close()


def test_check_object_exists_false_for_missing_table():
    a = SqliteAdapter()
    result = check_object_exists(a, "nope")
    assert result.verified is True
    assert result.exists is False
    assert result.error is not None
    assert result.info is None
    a.close()


def test_check_object_exists_unverified_when_adapter_unreachable():
    # An already-configured source found unreachable degrades to manual
    # entry; the caller passes adapter=None rather than a false negative.
    result = check_object_exists(None, "whatever")
    assert result.verified is False
    assert result.exists is None
    assert result.info is None
