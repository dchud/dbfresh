import pytest

from dbfresh.config import load_config


def _write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_load_config_parses_calendar_block(tmp_path):
    path = _write(
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
    path = _write(tmp_path, "sources: {}\nchecks: []\n")
    cfg = load_config(path, env={})
    assert cfg.calendar is None


_CALENDAR_BLOCK = "calendar:\n  timezone: America/New_York\n"


def test_load_config_parses_by_weekday_and_on_holiday(tmp_path):
    path = _write(
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
    path = _write(
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


def test_load_config_skip_off_schedule_defaults_false(tmp_path):
    path = _write(
        tmp_path,
        _CALENDAR_BLOCK
        + """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: row_count
    expect: { max: 5 }
""",
    )
    cfg = load_config(path, env={})
    assert cfg.checks[0].skip_off_schedule is False


def test_by_weekday_without_calendar_block_is_a_validation_error(tmp_path):
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
    by_weekday: { mon: { max: 10 } }
""",
    )
    with pytest.raises(ValueError):
        load_config(path, env={})


def test_on_holiday_without_calendar_block_is_a_validation_error(tmp_path):
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
    on_holiday: { max: 10 }
""",
    )
    with pytest.raises(ValueError):
        load_config(path, env={})


def test_calendar_business_without_calendar_block_is_a_validation_error(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: freshness
    column: created_at
    expect: { max_lag: 26h }
    calendar: business
""",
    )
    with pytest.raises(ValueError):
        load_config(path, env={})


def test_defaults_skip_off_schedule_applies_to_every_check(tmp_path):
    path = _write(
        tmp_path,
        _CALENDAR_BLOCK
        + """
defaults:
  skip_off_schedule: true
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: row_count
    expect: { max: 5 }
""",
    )
    cfg = load_config(path, env={})
    assert cfg.checks[0].skip_off_schedule is True


def test_per_check_skip_off_schedule_overrides_default(tmp_path):
    path = _write(
        tmp_path,
        _CALENDAR_BLOCK
        + """
defaults:
  skip_off_schedule: true
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: row_count
    expect: { max: 5 }
    skip_off_schedule: false
""",
    )
    cfg = load_config(path, env={})
    assert cfg.checks[0].skip_off_schedule is False


def test_skip_on_holiday_is_an_alias_for_skip_off_schedule(tmp_path):
    path = _write(
        tmp_path,
        _CALENDAR_BLOCK
        + """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: row_count
    expect: { max: 5 }
    skip_on_holiday: true
""",
    )
    cfg = load_config(path, env={})
    assert cfg.checks[0].skip_off_schedule is True


def test_defaults_skip_on_holiday_alias_applies_to_every_check(tmp_path):
    path = _write(
        tmp_path,
        _CALENDAR_BLOCK
        + """
defaults:
  skip_on_holiday: true
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: row_count
    expect: { max: 5 }
""",
    )
    cfg = load_config(path, env={})
    assert cfg.checks[0].skip_off_schedule is True


def test_per_check_skip_on_holiday_false_overrides_default_true(tmp_path):
    path = _write(
        tmp_path,
        _CALENDAR_BLOCK
        + """
defaults:
  skip_on_holiday: true
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: row_count
    expect: { max: 5 }
    skip_on_holiday: false
""",
    )
    cfg = load_config(path, env={})
    assert cfg.checks[0].skip_off_schedule is False


def test_skip_off_schedule_without_calendar_block_is_a_validation_error(tmp_path):
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
    skip_off_schedule: true
""",
    )
    with pytest.raises(ValueError):
        load_config(path, env={})


def test_unknown_weekday_key_in_by_weekday_is_a_validation_error(tmp_path):
    path = _write(
        tmp_path,
        _CALENDAR_BLOCK
        + """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: row_count
    expect: { max: 5 }
    by_weekday: { funday: { max: 10 } }
""",
    )
    with pytest.raises(ValueError):
        load_config(path, env={})


def test_unsupported_calendar_mode_is_a_validation_error(tmp_path):
    path = _write(
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
    calendar: lunar
""",
    )
    with pytest.raises(ValueError):
        load_config(path, env={})
