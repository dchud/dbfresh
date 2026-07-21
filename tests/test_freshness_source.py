"""freshness_source dispatch: the two DESCRIBE origins and run-time view
rejection.

Column-origin freshness end-to-end is covered by test_freshness.py against
the real sqlite adapter. The DESCRIBE origins are Databricks-only and take
no live warehouse in this suite, so they're exercised here against a fake
adapter exposing only what the dispatch needs: describe() and
describe_history_last_modified().
"""

from datetime import UTC, datetime

from dbfresh.adapters.base import ObjectInfo
from dbfresh.adapters.databricks import DatabricksDialect
from dbfresh.checks import Check, parse_expectation
from dbfresh.engine import Status, evaluate_check


class _FakeDescribeAdapter:
    def __init__(
        self, last_modified=None, history_last_modified=None, is_view=False
    ):
        self.dialect = DatabricksDialect()
        self._last_modified = last_modified
        self._history_last_modified = history_last_modified
        self._is_view = is_view
        self.history_calls = 0

    def describe(self, obj):
        return ObjectInfo(
            columns=[],
            is_view=self._is_view,
            last_modified=self._last_modified,
        )

    def describe_history_last_modified(self, obj):
        self.history_calls += 1
        return self._history_last_modified


def _check(freshness_source):
    return Check(
        source="s",
        object="main.gold.t",
        metric="freshness",
        freshness_source=freshness_source,
        expect=parse_expectation({"max_lag": "26h"}),
    )


def test_describe_detail_dispatches_to_describe_last_modified():
    when = datetime(2026, 7, 9, 10, 0, tzinfo=UTC)
    now = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)  # 34h later
    adapter = _FakeDescribeAdapter(last_modified=when)
    result = evaluate_check(_check("describe_detail"), adapter, now=now)
    assert result.status == Status.FAIL  # 34h > the 26h max_lag
    assert result.value == 34 * 3600
    assert adapter.history_calls == 0


def test_describe_history_dispatches_to_describe_history_last_modified():
    when = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
    now = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)  # 10h later
    adapter = _FakeDescribeAdapter(history_last_modified=when)
    result = evaluate_check(_check("describe_history"), adapter, now=now)
    assert result.status == Status.OK
    assert result.value == 10 * 3600
    assert adapter.history_calls == 1


def test_describe_origin_check_carries_no_column():
    check = _check("describe_detail")
    assert check.column is None


def test_describe_origin_empty_result_is_fail_not_error():
    adapter = _FakeDescribeAdapter(last_modified=None)
    now = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)
    result = evaluate_check(_check("describe_detail"), adapter, now=now)
    assert result.status == Status.FAIL


def test_describe_detail_against_a_view_errors_at_run_time():
    when = datetime(2026, 7, 10, tzinfo=UTC)
    adapter = _FakeDescribeAdapter(last_modified=when, is_view=True)
    now = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)
    result = evaluate_check(_check("describe_detail"), adapter, now=now)
    assert result.status == Status.ERROR
    assert "view" in result.error


def test_describe_history_against_a_view_errors_at_run_time():
    when = datetime(2026, 7, 10, tzinfo=UTC)
    adapter = _FakeDescribeAdapter(history_last_modified=when, is_view=True)
    now = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)
    result = evaluate_check(_check("describe_history"), adapter, now=now)
    assert result.status == Status.ERROR
    assert "view" in result.error
    assert adapter.history_calls == 0  # rejected before the history query runs
