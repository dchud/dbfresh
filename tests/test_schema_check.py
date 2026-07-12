from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.checks import Check, check_id, fingerprint_columns, parse_expectation
from dbfresh.engine import Status, evaluate_check, run_checks
from dbfresh.store import Store


def _adapter_with_table(ddl):
    a = SqliteAdapter()
    a.rows(ddl)
    return a


def _schema_check(**overrides):
    fields = {
        "source": "s",
        "object": "t",
        "metric": "schema",
        "expect": parse_expectation({"unchanged": True}, metric="schema"),
    }
    fields.update(overrides)
    return Check(**fields)


def test_schema_check_without_store_always_passes():
    a = _adapter_with_table("CREATE TABLE t (id INTEGER, name TEXT)")
    check = _schema_check()
    result = evaluate_check(check, a)
    assert result.status == Status.OK
    assert result.value  # fingerprint recorded as the observed value
    a.close()


def test_schema_check_first_run_establishes_baseline(tmp_path):
    a = _adapter_with_table("CREATE TABLE t (id INTEGER, name TEXT)")
    store = Store(tmp_path / "obs.db")
    check = _schema_check()
    result = evaluate_check(check, a, store=store)
    assert result.status == Status.OK
    a.close()
    store.close()


def test_schema_check_unchanged_passes_when_fingerprint_matches(tmp_path):
    a = _adapter_with_table("CREATE TABLE t (id INTEGER, name TEXT)")
    store = Store(tmp_path / "obs.db")
    check = _schema_check()
    run_id = store.start_run()
    first = evaluate_check(check, a, store=store)
    store.record_observation(run_id, first)

    second = evaluate_check(check, a, store=store)
    assert second.status == Status.OK
    a.close()
    store.close()


def test_schema_check_unchanged_fails_on_added_column(tmp_path):
    a = _adapter_with_table("CREATE TABLE t (id INTEGER, name TEXT)")
    store = Store(tmp_path / "obs.db")
    check = _schema_check()
    run_id = store.start_run()
    first = evaluate_check(check, a, store=store)
    store.record_observation(run_id, first)

    a.rows("ALTER TABLE t ADD COLUMN email TEXT")
    second = evaluate_check(check, a, store=store)
    assert second.status == Status.FAIL
    assert second.diff == ["+ email (TEXT)"]
    a.close()
    store.close()


def test_schema_check_unchanged_fails_on_removed_column(tmp_path):
    a = _adapter_with_table("CREATE TABLE t (id INTEGER, name TEXT, extra TEXT)")
    store = Store(tmp_path / "obs.db")
    check = _schema_check()
    run_id = store.start_run()
    first = evaluate_check(check, a, store=store)
    store.record_observation(run_id, first)

    a.rows("ALTER TABLE t DROP COLUMN extra")
    second = evaluate_check(check, a, store=store)
    assert second.status == Status.FAIL
    assert second.diff == ["- extra (TEXT)"]
    a.close()
    store.close()


def test_schema_check_unchanged_fails_on_retyped_column(tmp_path):
    a = _adapter_with_table("CREATE TABLE t (id INTEGER, amount REAL)")
    store = Store(tmp_path / "obs.db")
    check = _schema_check()
    run_id = store.start_run()
    first = evaluate_check(check, a, store=store)
    store.record_observation(run_id, first)

    a.rows("DROP TABLE t")
    a.rows("CREATE TABLE t (id INTEGER, amount TEXT)")
    second = evaluate_check(check, a, store=store)
    assert second.status == Status.FAIL
    assert second.diff == ["~ amount (REAL -> TEXT)"]
    a.close()
    store.close()


def test_schema_check_warn_severity_yields_warn_not_fail(tmp_path):
    a = _adapter_with_table("CREATE TABLE t (id INTEGER)")
    store = Store(tmp_path / "obs.db")
    check = _schema_check(severity="warn")
    run_id = store.start_run()
    first = evaluate_check(check, a, store=store)
    store.record_observation(run_id, first)

    a.rows("ALTER TABLE t ADD COLUMN extra TEXT")
    second = evaluate_check(check, a, store=store)
    assert second.status == Status.WARN
    a.close()
    store.close()


def test_schema_check_equals_pinned_fingerprint_passes():
    a = _adapter_with_table("CREATE TABLE t (id INTEGER, name TEXT)")
    pinned = fingerprint_columns(a.describe("t").columns)
    check = _schema_check(expect=parse_expectation({"equals": pinned}, metric="schema"))
    result = evaluate_check(check, a)
    assert result.status == Status.OK
    a.close()


def test_schema_check_equals_pinned_fingerprint_fails_on_drift():
    a = _adapter_with_table("CREATE TABLE t (id INTEGER, name TEXT)")
    check = _schema_check(
        expect=parse_expectation({"equals": "id:INTEGER"}, metric="schema")
    )
    result = evaluate_check(check, a)
    assert result.status == Status.FAIL
    assert result.diff == ["+ name (TEXT)"]
    a.close()


def test_schema_check_missing_object_is_error():
    a = SqliteAdapter()  # table never created
    check = _schema_check(object="missing")
    result = evaluate_check(check, a)
    assert result.status == Status.ERROR
    assert result.error is not None
    a.close()


def test_schema_check_carries_check_id():
    a = _adapter_with_table("CREATE TABLE t (id INTEGER)")
    check = _schema_check()
    result = evaluate_check(check, a)
    assert result.check_id == check_id(check)
    a.close()


def test_run_checks_threads_store_through_for_schema(tmp_path):
    a = _adapter_with_table("CREATE TABLE t (id INTEGER)")
    store = Store(tmp_path / "obs.db")
    check = _schema_check()
    run_id = store.start_run()
    first_run = run_checks({"s": a}, [check], store=store)
    store.record_observation(run_id, first_run.results[0])

    second_run = run_checks({"s": a}, [check], store=store)
    assert second_run.results[0].status == Status.OK
    a.close()
    store.close()
