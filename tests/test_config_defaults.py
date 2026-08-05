"""defaults: merging beyond skip_off_schedule (severity, calendar, where,

allow_empty) — a per-check value always overrides the default.
"""

import pytest
from helpers import write_config

from dbfresh.config import load_config

_CALENDAR_BLOCK = "calendar:\n  timezone: America/New_York\n"


def _load(tmp_path, default, check_field, *, prelude="", check_body=None):
    body = check_body or "    metric: row_count\n    expect: { max: 5 }\n"
    field = f"    {check_field}\n" if check_field else ""
    path = write_config(
        tmp_path,
        f"""{prelude}defaults:
  {default}
sources:
  s: {{ type: sqlite, database: ":memory:" }}
checks:
  - source: s
    object: t
{body}{field}""",
    )
    return load_config(path, env={})


@pytest.mark.parametrize(
    ("default", "check_field", "attribute", "expected"),
    [
        pytest.param(
            "severity: warn", "", "severity", "warn", id="severity-default"
        ),
        pytest.param(
            "severity: warn",
            "severity: error",
            "severity",
            "error",
            id="severity-per-check",
        ),
        pytest.param(
            "where: \"region = 'US'\"",
            "",
            "where",
            "region = 'US'",
            id="where-default",
        ),
        pytest.param(
            "where: \"region = 'US'\"",
            "where: \"region = 'EU'\"",
            "where",
            "region = 'EU'",
            id="where-per-check",
        ),
        pytest.param(
            "allow_empty: true",
            "",
            "allow_empty",
            True,
            id="allow_empty-default",
        ),
        pytest.param(
            "allow_empty: true",
            "allow_empty: false",
            "allow_empty",
            False,
            id="allow_empty-per-check",
        ),
    ],
)
def test_default_applies_unless_the_check_sets_its_own(
    tmp_path, default, check_field, attribute, expected
):
    cfg = _load(tmp_path, default, check_field)
    assert getattr(cfg.checks[0], attribute) == expected


@pytest.mark.parametrize(
    ("check_field", "expected"),
    [
        pytest.param("", "business", id="default-applies"),
        # an explicit null overrides the default rather than falling back to it
        pytest.param("calendar: null", None, id="per-check-null-overrides"),
    ],
)
def test_default_calendar_mode(tmp_path, check_field, expected):
    cfg = _load(
        tmp_path,
        "calendar: business",
        check_field,
        prelude=_CALENDAR_BLOCK,
        check_body=(
            "    metric: freshness\n"
            "    column: created_at\n"
            "    expect: { max_lag: 26h }\n"
        ),
    )
    assert cfg.checks[0].calendar == expected
