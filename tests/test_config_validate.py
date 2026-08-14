"""`validate_config`: every problem in a config, collected and attributed.

Unlike `load_config`, which raises on the first problem it finds,
`validate_config` never stops early for a check-level or source-level
problem -- it collects every one of them into a `ConfigProblem`, each
naming the file(s) it came from, and returns them alongside the `Config`
it was still able to resolve. Only a problem that blocks resolving the
check set at all (bad YAML, an unmatched `include:` glob, ...) still
raises `ConfigError`, exactly as `load_config` does.
"""

import pytest
from helpers import write_config, write_file

from dbfresh.config import ConfigError, load_config, validate_config


def test_several_malformed_checks_are_all_reported_not_just_first(tmp_path):
    # Three distinct failure classes in one config: a validate-time
    # problem (no expectation -- the check still builds), a build-time
    # KeyError (no `object:` at all), and a build-time TypeError (`expect`
    # isn't a mapping). All three must show up, not just the first.
    path = write_config(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: row_count
  - source: s
    metric: row_count
    expect: { max: 5 }
  - source: s
    object: u
    metric: row_count
    expect: 5
""",
    )

    result = validate_config(path, env={})

    messages = [p.message for p in result.problems]
    assert len(messages) == 3
    assert any("no expectation" in m for m in messages)
    assert any("missing required field: 'object'" in m for m in messages)
    assert any(
        "invalid expectation: object of type 'int' has no len()" in m
        for m in messages
    )


def test_problems_are_attributed_to_the_correct_file(tmp_path):
    root = write_file(
        tmp_path / "config.yaml",
        """
sources:
  s: { type: sqlite, database: ":memory:" }
include:
  - checks/*.yaml
checks:
  - source: s
    object: root_bad
    metric: row_count
    expect: 5
""",
    )
    included = write_file(
        tmp_path / "checks" / "a.yaml",
        """
checks:
  - source: s
    object: included_bad
    metric: row_count
    colum: x
    expect: { max: 5 }
""",
    )

    result = validate_config(root, env={})

    by_file: dict = {}
    for problem in result.problems:
        for file in problem.files:
            by_file.setdefault(file, []).append(problem.message)

    assert len(result.problems) == 2
    assert any("invalid expectation" in m for m in by_file[root])
    assert any("unknown check field" in m for m in by_file[included])


def test_duplicate_check_id_across_two_files_names_both(tmp_path):
    root = write_file(
        tmp_path / "config.yaml",
        """
sources:
  s: { type: sqlite, database: ":memory:" }
include:
  - checks/*.yaml
checks:
  - source: s
    object: t
    metric: row_count
    id: dup
    expect: { max: 5 }
""",
    )
    included = write_file(
        tmp_path / "checks" / "a.yaml",
        """
checks:
  - source: s
    object: u
    metric: row_count
    id: dup
    expect: { max: 5 }
""",
    )

    result = validate_config(root, env={})

    (problem,) = [
        p for p in result.problems if "duplicate check_id" in p.message
    ]
    assert set(problem.files) == {root, included}


def test_every_undefined_var_is_reported_not_just_first(tmp_path):
    path = write_config(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: "${DB_PATH}" }
checks:
  - source: s
    object: t
    metric: row_count
    where: "region = '${REGION}'"
    expect: { max: 5 }
""",
    )

    result = validate_config(path, env={})

    var_messages = {
        p.message
        for p in result.problems
        if "undefined environment variable" in p.message
    }
    assert var_messages == {
        "undefined environment variable: DB_PATH",
        "undefined environment variable: REGION",
    }


def test_include_glob_matching_nothing_still_raises(tmp_path):
    # Cannot be collected past: without resolving includes, the check set
    # itself is unknown, so this stays a single stop-the-world ConfigError
    # rather than a collected ConfigProblem.
    root = write_file(
        tmp_path / "config.yaml",
        """
sources:
  s: { type: sqlite, database: ":memory:" }
include:
  - checks/nope-*.yaml
checks: []
""",
    )

    with pytest.raises(ConfigError, match="glob matched no files"):
        validate_config(root, env={})


def test_clean_config_has_no_problems(tmp_path):
    path = write_config(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    object: t
    metric: row_count
    expect: { max: 5 }
""",
    )

    result = validate_config(path, env={})

    assert result.ok
    assert result.problems == []
    assert [c.object for c in result.config.checks] == ["t"]


def test_load_config_single_malformed_check_message_is_unchanged(tmp_path):
    # Regression guard for the hard constraint: validate_config's
    # collecting path must not change load_config's own contract.
    # load_config still raises on the first malformed check, with the
    # exact message it always has, and never inspects the second one.
    path = write_config(
        tmp_path,
        """
sources:
  s: { type: sqlite, database: ":memory:" }
checks:
  - source: s
    metric: row_count
    expect: { max: 5 }
  - source: s
    object: t2
    metric: row_count
    expect: { max: 5 }
""",
    )

    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})

    assert str(excinfo.value) == "missing required field: 'object'"
