"""defaults: merging beyond skip_off_schedule (severity, calendar, where,

allow_empty) — a per-check value always overrides the default (§12.1).
"""

from dbfresh.config import load_config


def _write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_default_severity_applies_to_every_check(tmp_path):
    path = _write(
        tmp_path,
        """
defaults:
  severity: warn
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
    assert cfg.checks[0].severity == "warn"


def test_per_check_severity_overrides_default(tmp_path):
    path = _write(
        tmp_path,
        """
defaults:
  severity: warn
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: row_count
    expect: { max: 5 }
    severity: error
""",
    )
    cfg = load_config(path, env={})
    assert cfg.checks[0].severity == "error"


def test_default_where_applies_to_every_check(tmp_path):
    path = _write(
        tmp_path,
        """
defaults:
  where: "region = 'US'"
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
    assert cfg.checks[0].where == "region = 'US'"


def test_per_check_where_overrides_default(tmp_path):
    path = _write(
        tmp_path,
        """
defaults:
  where: "region = 'US'"
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: row_count
    expect: { max: 5 }
    where: "region = 'EU'"
""",
    )
    cfg = load_config(path, env={})
    assert cfg.checks[0].where == "region = 'EU'"


def test_default_allow_empty_applies_to_every_check(tmp_path):
    path = _write(
        tmp_path,
        """
defaults:
  allow_empty: true
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
    assert cfg.checks[0].allow_empty is True


def test_per_check_allow_empty_false_overrides_default_true(tmp_path):
    path = _write(
        tmp_path,
        """
defaults:
  allow_empty: true
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: row_count
    expect: { max: 5 }
    allow_empty: false
""",
    )
    cfg = load_config(path, env={})
    assert cfg.checks[0].allow_empty is False


def test_default_calendar_mode_applies_to_every_check(tmp_path):
    path = _write(
        tmp_path,
        """
calendar:
  timezone: America/New_York
defaults:
  calendar: business
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: freshness
    column: created_at
    expect: { max_lag: 26h }
""",
    )
    cfg = load_config(path, env={})
    assert cfg.checks[0].calendar == "business"


def test_per_check_calendar_mode_null_overrides_default_business(tmp_path):
    path = _write(
        tmp_path,
        """
calendar:
  timezone: America/New_York
defaults:
  calendar: business
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: freshness
    column: created_at
    expect: { max_lag: 26h }
    calendar: null
""",
    )
    cfg = load_config(path, env={})
    # an explicit null overrides the default rather than falling back to it
    assert cfg.checks[0].calendar is None
