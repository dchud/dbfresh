from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.checks import Check, parse_expectation
from dbfresh.engine import Status, exit_code, run_checks, worst_status


def test_worst_status_orders_by_severity():
    assert worst_status([Status.OK, Status.WARN]) == Status.WARN
    assert worst_status([Status.WARN, Status.FAIL]) == Status.FAIL
    assert worst_status([Status.FAIL, Status.ERROR]) == Status.ERROR


def test_worst_status_treats_skipped_as_clear():
    assert worst_status([Status.OK, Status.SKIPPED]) == Status.OK


def test_worst_status_empty_is_ok():
    assert worst_status([]) == Status.OK


def test_exit_codes():
    assert exit_code(Status.OK) == 0
    assert exit_code(Status.SKIPPED) == 0
    assert exit_code(Status.WARN) == 1
    assert exit_code(Status.FAIL) == 2
    assert exit_code(Status.ERROR) == 3


def test_run_checks_aggregates_worst_status():
    a = SqliteAdapter()
    a.rows("CREATE TABLE t (id INTEGER)")
    a.rows("INSERT INTO t (id) VALUES (1), (2), (3)")
    ok = Check(
        source="s",
        object="t",
        metric="row_count",
        expect=parse_expectation({"between": [1, 10]}),
    )
    bad = Check(
        source="s",
        object="t",
        metric="row_count",
        expect=parse_expectation({"max": 1}),
    )
    run = run_checks({"s": a}, [ok, bad])
    assert len(run.results) == 2
    assert run.status == Status.FAIL
    a.close()
