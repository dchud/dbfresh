"""Configurator safety and degradation: connection test, existence
check, and unverified-manual-entry degradation."""

from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.configurator import check_object_exists, probe_connection, probe_new_source


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


def test_probe_new_source_interpolates_env_before_probing(monkeypatch):
    # A brand-new source's raw params may hold ${VAR} secrets; the probe
    # must run against the resolved value, not the literal placeholder.
    monkeypatch.setenv("DBFRESH_TEST_TOKEN", "sekret")
    captured = {}

    def fake_probe(type_, params):
        captured["type_"] = type_
        captured["params"] = params
        return probe_connection("sqlite", {"database": ":memory:"})

    monkeypatch.setattr("dbfresh.configurator.probe_connection", fake_probe)

    result, resolved = probe_new_source("sqlite", {"token": "${DBFRESH_TEST_TOKEN}"})

    assert result.ok is True
    assert resolved == {"token": "sekret"}
    assert captured["params"] == {"token": "sekret"}


def test_probe_new_source_fails_cleanly_on_undefined_variable(monkeypatch):
    monkeypatch.delenv("DBFRESH_TEST_TOKEN_UNSET", raising=False)
    result, _resolved = probe_new_source(
        "sqlite", {"token": "${DBFRESH_TEST_TOKEN_UNSET}"}
    )
    assert result.ok is False
    assert "DBFRESH_TEST_TOKEN_UNSET" in result.error
