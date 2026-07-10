from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.checks import Check, parse_expectation
from dbfresh.engine import Status, evaluate_check


def _adapter_with_rows(n):
    a = SqliteAdapter()
    a.rows("CREATE TABLE t (id INTEGER)")
    for i in range(n):
        a.rows(f"INSERT INTO t (id) VALUES ({i})")
    return a


def test_row_count_within_range_is_ok():
    a = _adapter_with_rows(5)
    check = Check(
        source="s",
        object="t",
        metric="row_count",
        expect=parse_expectation({"between": [1, 10]}),
    )
    result = evaluate_check(check, a)
    assert result.status == Status.OK
    assert result.value == 5
    a.close()


def test_row_count_out_of_range_fails():
    a = _adapter_with_rows(20)
    check = Check(
        source="s",
        object="t",
        metric="row_count",
        expect=parse_expectation({"max": 10}),
    )
    result = evaluate_check(check, a)
    assert result.status == Status.FAIL
    assert result.value == 20
    a.close()


def test_warn_severity_yields_warn_not_fail():
    a = _adapter_with_rows(20)
    check = Check(
        source="s",
        object="t",
        metric="row_count",
        expect=parse_expectation({"max": 10}),
        severity="warn",
    )
    assert evaluate_check(check, a).status == Status.WARN
    a.close()


def test_query_error_is_error_status():
    a = SqliteAdapter()  # table never created
    check = Check(
        source="s",
        object="nonexistent",
        metric="row_count",
        expect=parse_expectation({"max": 10}),
    )
    result = evaluate_check(check, a)
    assert result.status == Status.ERROR
    assert result.error is not None
    a.close()
