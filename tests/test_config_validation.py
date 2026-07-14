"""Up-front config validation: every problem collected and reported at once.

`load_config` validates the composed check list before any connection is
attempted. Each rule below names the offending check's source/object/metric
in its message; when a config has several problems, every one of them shows
up in the single raised `ConfigError` rather than only the first.
"""

import pytest

from dbfresh.config import load_config


def _write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_unknown_metric_is_a_clean_validation_error(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: rowcount
    expect: { max: 5 }
""",
    )
    with pytest.raises(ValueError, match="unknown metric: 'rowcount'"):
        load_config(path, env={})


def test_null_rate_without_column_is_a_validation_error(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: null_rate
    expect: { max: 0.1 }
""",
    )
    with pytest.raises(ValueError, match="requires 'column'"):
        load_config(path, env={})


@pytest.mark.parametrize("metric", ["sum", "avg", "min", "max"])
def test_aggregate_metric_without_column_is_a_validation_error(tmp_path, metric):
    path = _write(
        tmp_path,
        f"""
sources:
  s: {{ type: sqlite, database: ":memory:" }}
checks:
  - source: s
    object: t
    metric: {metric}
    expect: {{ max: 5 }}
""",
    )
    with pytest.raises(ValueError, match="requires 'column'"):
        load_config(path, env={})


def test_duplicate_count_without_key_is_a_validation_error(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: duplicate_count
    expect: { max: 0 }
""",
    )
    with pytest.raises(ValueError, match="requires 'key'"):
        load_config(path, env={})


def test_freshness_column_source_without_column_is_a_validation_error(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: freshness
    expect: { max_lag: 26h }
""",
    )
    with pytest.raises(ValueError, match="requires 'column'"):
        load_config(path, env={})


def test_metric_check_without_expectation_is_a_validation_error(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: row_count
""",
    )
    with pytest.raises(ValueError, match="no expectation"):
        load_config(path, env={})


def test_unknown_check_field_is_a_validation_error(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: row_count
    colum: t
    expect: { max: 5 }
""",
    )
    with pytest.raises(ValueError, match="unknown check field"):
        load_config(path, env={})


def test_bad_severity_is_a_validation_error(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: row_count
    expect: { max: 5 }
    severity: yikes
""",
    )
    with pytest.raises(ValueError, match="severity must be"):
        load_config(path, env={})


def test_max_lag_on_non_freshness_metric_is_a_validation_error(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: row_count
    expect: { max_lag: 26h }
""",
    )
    with pytest.raises(ValueError, match="max_lag"):
        load_config(path, env={})


def test_several_problems_are_all_reported_at_once(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: rowcount
    expect: { max: 5 }
  - source: s
    object: u
    metric: null_rate
    expect: { max: 0.1 }
  - source: s
    object: v
    metric: row_count
    expect: { max: 5 }
    severity: yikes
""",
    )
    with pytest.raises(ValueError) as excinfo:
        load_config(path, env={})
    message = str(excinfo.value)
    assert "unknown metric: 'rowcount'" in message
    assert "requires 'column'" in message
    assert "severity must be" in message


def test_check_with_no_metric_assert_or_assert_sql_is_a_validation_error(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
""",
    )
    with pytest.raises(ValueError, match="none of metric, assert, or assert_sql"):
        load_config(path, env={})


def test_check_with_metric_and_assert_is_a_validation_error(tmp_path):
    # A check with both a metric and an assert: silently ran only the
    # assert in dispatch, dropping the metric expectation with no error.
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: row_count
    expect: { max: 5 }
    assert: "x >= 0"
""",
    )
    with pytest.raises(ValueError, match="more than one"):
        load_config(path, env={})


def test_check_with_assert_and_assert_sql_is_a_validation_error(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    assert: "x >= 0"
    assert_sql: "SELECT * FROM t WHERE x < 0"
""",
    )
    with pytest.raises(ValueError, match="more than one"):
        load_config(path, env={})


def test_assert_sql_check_loads_and_needs_no_expectation(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    assert_sql: "SELECT * FROM t WHERE amount < 0"
""",
    )
    cfg = load_config(path, env={})
    assert cfg.checks[0].assert_sql == "SELECT * FROM t WHERE amount < 0"


def test_single_problem_message_is_not_wrapped_in_a_multi_error_summary(tmp_path):
    # Exactly one problem: the message is that problem's own text, not
    # "N problems found" framing -- keeps the single-error case's message
    # identical to before this validation pass existed.
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: other
    object: t
    metric: row_count
    expect: { max: 5 }
""",
    )
    with pytest.raises(ValueError) as excinfo:
        load_config(path, env={})
    assert str(excinfo.value) == "check references unknown source: 'other'"
