from datetime import UTC, datetime

from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.checks import Check
from dbfresh.engine import Result, RunResult, Status, evaluate_check
from dbfresh.report import render_digest


def _adapter_with_negatives():
    a = SqliteAdapter()
    a.rows("CREATE TABLE fct (sale_id INTEGER, amount REAL)")
    a.rows("INSERT INTO fct VALUES (1, 10.0), (2, -5.0), (3, -1.0)")
    return a


def test_assertion_with_violations_fails():
    a = _adapter_with_negatives()
    check = Check(source="s", object="fct", assert_="amount >= 0")
    result = evaluate_check(check, a)
    assert result.status == Status.FAIL
    assert result.value == 2
    assert len(result.samples) == 2
    a.close()


def test_assertion_with_no_violations_is_ok():
    a = SqliteAdapter()
    a.rows("CREATE TABLE fct (amount REAL)")
    a.rows("INSERT INTO fct VALUES (1.0), (2.0)")
    check = Check(source="s", object="fct", assert_="amount >= 0")
    result = evaluate_check(check, a)
    assert result.status == Status.OK
    assert result.value == 0
    a.close()


def test_digest_shows_assertion_violations():
    run = RunResult(
        results=[
            Result(
                source="warehouse",
                object="dbo.fct_sales",
                metric=None,
                status=Status.FAIL,
                value=3,
                label="assert amount >= 0",
                samples=[{"sale_id": 88213, "amount": -42.0}],
            )
        ],
        status=Status.FAIL,
    )
    text = render_digest(run, now=datetime(2026, 7, 10, tzinfo=UTC))
    assert "✗ warehouse.dbo.fct_sales · assert amount >= 0" in text
    assert "3 row(s) violate the constraint" in text
    assert "sale_id=88213" in text
