from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.checks import Check, parse_expectation
from dbfresh.engine import (
    Status,
    _error_result,
    _result,
    _verdict,
    evaluate_check,
    run_checks,
)


def _adapter_with_rows(n):
    a = SqliteAdapter()
    a.rows("CREATE TABLE t (id INTEGER)")
    for i in range(n):
        a.rows(f"INSERT INTO t (id) VALUES ({i})")
    return a


def test_error_result_never_stores_a_blank_message():
    # NotImplementedError() stringifies to "" -- the swallowed-error case
    # that made a schema ERROR undebuggable. _error_result must fall back to
    # something identifying (the exception type) rather than storing "".
    check = Check(source="s", object="o", metric="schema")
    result = _error_result(check, NotImplementedError())
    assert result.status is Status.ERROR
    assert result.error
    assert "NotImplementedError" in result.error


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


def test_unsupported_metric_is_error_status_not_a_crash():
    # compile_metric_sql runs outside the per-metric try/except blocks, so
    # an unvalidated metric name must be caught by evaluate_check's own
    # outer safety net instead of propagating out of a worker thread.
    a = _adapter_with_rows(3)
    check = Check(source="s", object="t", metric="not_a_real_metric")
    result = evaluate_check(check, a)
    assert result.status == Status.ERROR
    assert result.error is not None
    assert result.check_id is not None
    assert result.object == "t"
    assert result.metric == "not_a_real_metric"
    a.close()


def test_verdict_ok_when_passed_regardless_of_severity():
    check = Check(source="s", object="t", metric="row_count", severity="warn")
    assert _verdict(check, passed=True) == Status.OK


def test_verdict_fail_when_not_passed_and_severity_is_error():
    check = Check(source="s", object="t", metric="row_count", severity="error")
    assert _verdict(check, passed=False) == Status.FAIL


def test_verdict_warn_when_not_passed_and_severity_is_warn():
    check = Check(source="s", object="t", metric="row_count", severity="warn")
    assert _verdict(check, passed=False) == Status.WARN


def test_result_defaults_object_metric_source_from_check():
    check = Check(source="s", object="t", metric="row_count")
    result = _result(check, Status.OK, value=5)
    assert result.object == "t"
    assert result.metric == "row_count"
    assert result.source == "s"
    assert result.value == 5


def test_result_field_override_wins_over_check_default():
    check = Check(source="s", object="t", metric="row_count")
    result = _result(check, Status.OK, metric=None, label="assert x")
    assert result.metric is None
    assert result.label == "assert x"


def test_error_result_carries_exception_message_and_check_defaults():
    check = Check(source="s", object="t", metric="row_count")
    result = _error_result(check, ValueError("boom"))
    assert result.status == Status.ERROR
    assert result.error == "boom"
    assert result.object == "t"
    assert result.metric == "row_count"
    assert result.source == "s"


def test_result_tier_is_table_when_no_column_or_key_named():
    check = Check(source="s", object="t", metric="row_count")
    result = _result(check, Status.OK, value=5)
    assert result.tier == "table"


def test_result_tier_is_column_when_column_named():
    check = Check(source="s", object="t", metric="null_rate", column="email")
    result = _result(check, Status.OK, value=0.1)
    assert result.tier == "column"


def test_result_tier_is_column_when_key_named():
    check = Check(source="s", object="t", metric="duplicate_count", key="id")
    result = _result(check, Status.OK, value=0)
    assert result.tier == "column"


def test_run_checks_invokes_on_result_once_per_check():
    a = _adapter_with_rows(3)
    checks = [
        Check(
            source="s",
            object="t",
            metric="row_count",
            id="first",
            expect=parse_expectation({"between": [1, 10]}),
        ),
        Check(
            source="s",
            object="t",
            metric="row_count",
            id="second",
            expect=parse_expectation({"between": [1, 10]}),
        ),
    ]
    seen = []
    run_checks({"s": a}, checks, on_result=seen.append)
    assert len(seen) == 2
    a.close()
