from datetime import UTC, datetime

import pytest
from helpers import write_config

from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.config import load_config
from dbfresh.engine import Status, evaluate_check


def test_load_config_parses_calendar_block(tmp_path):
    path = write_config(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
calendar:
  timezone: America/New_York
  workdays: [mon, tue, wed, thu, fri]
  holidays: { country: US }
checks: []
""",
    )
    cfg = load_config(path, env={})
    assert cfg.calendar is not None
    assert cfg.calendar.timezone == "America/New_York"


def test_load_config_defaults_calendar_to_none(tmp_path):
    path = write_config(tmp_path, "sources: {}\nchecks: []\n")
    cfg = load_config(path, env={})
    assert cfg.calendar is None


_CALENDAR_BLOCK = "calendar:\n  timezone: America/New_York\n"

_ROW_COUNT = """
  - source: s
    object: t
    metric: row_count
    expect: { max: 5 }
"""

_FRESHNESS = """
  - source: s
    object: t
    metric: freshness
    column: created_at
    expect: { max_lag: 26h }
"""


def _load_one_check(tmp_path, check, *, prelude=""):
    """Load a single-source config whose only check is ``check``."""
    path = write_config(
        tmp_path,
        prelude
        + """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:"""
        + check,
    )
    return load_config(path, env={})


def test_load_config_parses_by_weekday_and_on_holiday(tmp_path):
    path = write_config(
        tmp_path,
        _CALENDAR_BLOCK
        + """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: row_count
    expect: { between: [1, 10] }
    by_weekday:
      mon: { max: 100 }
      sat: { max: 5 }
    on_holiday: { max: 5 }
""",
    )
    cfg = load_config(path, env={})
    check = cfg.checks[0]
    assert check.by_weekday["mon"].evaluate(50) is True
    assert check.by_weekday["sat"].evaluate(50) is False
    assert check.on_holiday.evaluate(5) is True


def test_load_config_parses_calendar_business_and_skip_off_schedule(tmp_path):
    path = write_config(
        tmp_path,
        _CALENDAR_BLOCK
        + """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: freshness
    column: created_at
    expect: { max_lag: 26h }
    calendar: business
    skip_off_schedule: true
""",
    )
    cfg = load_config(path, env={})
    check = cfg.checks[0]
    assert check.calendar == "business"
    assert check.skip_off_schedule is True


@pytest.mark.parametrize(
    ("default", "check_field", "expected"),
    [
        pytest.param("", "", False, id="unset-everywhere"),
        pytest.param(
            "skip_off_schedule: true", "", True, id="default-applies"
        ),
        pytest.param(
            "skip_off_schedule: true",
            "skip_off_schedule: false",
            False,
            id="per-check-overrides-default",
        ),
        pytest.param("", "skip_on_holiday: true", True, id="alias-per-check"),
        pytest.param("skip_on_holiday: true", "", True, id="alias-as-default"),
        pytest.param(
            "skip_on_holiday: true",
            "skip_on_holiday: false",
            False,
            id="alias-per-check-overrides-alias-default",
        ),
    ],
)
def test_skip_off_schedule_resolution(
    tmp_path, default, check_field, expected
):
    """``skip_on_holiday`` is an alias, so both spellings resolve the same."""
    defaults_block = f"defaults:\n  {default}\n" if default else ""
    field = f"    {check_field}\n" if check_field else ""
    cfg = _load_one_check(
        tmp_path, _ROW_COUNT + field, prelude=_CALENDAR_BLOCK + defaults_block
    )
    assert cfg.checks[0].skip_off_schedule is expected


@pytest.mark.parametrize(
    "check",
    [
        pytest.param(
            _ROW_COUNT + "    by_weekday: { mon: { max: 10 } }\n",
            id="by_weekday",
        ),
        pytest.param(
            _ROW_COUNT + "    on_holiday: { max: 10 }\n", id="on_holiday"
        ),
        pytest.param(
            _ROW_COUNT + "    skip_off_schedule: true\n",
            id="skip_off_schedule",
        ),
        pytest.param(
            _FRESHNESS + "    calendar: business\n", id="calendar-business"
        ),
    ],
)
def test_calendar_field_without_a_calendar_block_is_a_validation_error(
    tmp_path, check
):
    with pytest.raises(ValueError):
        _load_one_check(tmp_path, check)


@pytest.mark.parametrize(
    "check",
    [
        pytest.param(
            _ROW_COUNT + "    by_weekday: { funday: { max: 10 } }\n",
            id="unknown-weekday-key",
        ),
        pytest.param(
            _FRESHNESS + "    calendar: lunar\n",
            id="unsupported-calendar-mode",
        ),
    ],
)
def test_unrecognized_calendar_value_is_a_validation_error(tmp_path, check):
    with pytest.raises(ValueError):
        _load_one_check(tmp_path, check, prelude=_CALENDAR_BLOCK)


def test_skip_on_holiday_actually_skips_evaluation_on_a_holiday(tmp_path):
    path = write_config(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
calendar:
  timezone: UTC
  holidays: { extra: ["2026-07-06"] }
checks:
  - source: s
    object: t
    metric: row_count
    expect: { min: 1 }
    skip_on_holiday: true
""",
    )
    cfg = load_config(path, env={})
    adapter = SqliteAdapter()
    adapter.rows(
        "CREATE TABLE t (id INTEGER)"
    )  # 0 rows -- would FAIL if evaluated
    now = datetime(
        2026, 7, 6, 12, 0, tzinfo=UTC
    )  # Monday, the configured holiday

    result = evaluate_check(
        cfg.checks[0], adapter, now=now, calendar=cfg.calendar
    )

    assert result.status == Status.SKIPPED
    adapter.close()
