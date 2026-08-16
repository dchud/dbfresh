"""Load, interpolate, and validate check configuration."""

from __future__ import annotations

import inspect
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from dbfresh.adapters.base import validate_freshness_source
from dbfresh.adapters.factory import adapter_class_for, dialect_for_type
from dbfresh.calendar import WEEKDAY_NAMES, BusinessCalendar, build_calendar
from dbfresh.checks import Check, check_id, describe_check, parse_expectation
from dbfresh.registry import METRICS

_CHECK_CALENDAR_MODES = frozenset({"business"})
_FRESHNESS_SOURCES = frozenset(
    {"column", "describe_history", "describe_detail"}
)
_METRIC_REQUIRED = {spec.name: spec.required for spec in METRICS}
_VALID_SEVERITIES = frozenset({"error", "warn"})
_SOURCE_OWN_FIELDS = frozenset({"type", "timezone", "timeout"})
_CHECK_KEYS = frozenset(
    {
        "source",
        "object",
        "metric",
        "column",
        "key",
        "where",
        "assert",
        "assert_sql",
        "expect",
        "allow_empty",
        "severity",
        "id",
        "by_weekday",
        "on_holiday",
        "calendar",
        "skip_off_schedule",
        "skip_on_holiday",
        "freshness_source",
    }
)

# A `tables:` entry's own fields -- deliberately a separate set from
# _CHECK_KEYS rather than a union with it: a table entry and a check block
# are different things that happen to nest one inside the other, and
# keeping their key sets apart is what lets `description`/`tags`/
# `upstream`/`downstream` (a later addition, entry-level metadata with no
# check-block equivalent) get added to this set alone, without touching
# what a flat check accepts. `use`/`with`/`skip` are the same story for
# check_sets: table-entry fields that select and parameterize a named
# battery, with no equivalent on a hand-written check block.
_TABLE_ENTRY_KEYS = frozenset(
    {"source", "object", "checks", "use", "with", "skip"}
)

# The two fields a table entry states once for every check nested under
# it. A nested check repeating either is rejected outright rather than
# silently allowed to override -- if a nested value could win, "the table
# states it once" would stop being true, and a reader could no longer
# trust the table header without checking every check under it. A
# check_sets item is held to the same rule (see _expand_check_set): a set
# describes check bodies, never which table they belong to.
_TABLE_CHECK_OWN_FIELDS = frozenset({"source", "object"})

# A check_sets entry is always {with: {...}, checks: [...]} -- with:
# optional, checks: required -- never a bare list. One shape, not "a list
# or a mapping", so a stray list-shaped entry is exactly as wrong as a
# missing checks:.
_CHECK_SET_KEYS = frozenset({"with", "checks"})

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# {{ name }} references a with: parameter, distinct from ${VAR}'s
# environment secrets -- interpolate_env (above) runs first, over the
# whole parsed document, and never touches these; substitution happens
# later, per check_sets use:, once a table's own with: is known. Internal
# whitespace inside the braces is allowed (`{{ name }}` and `{{name}}`
# both match); the name itself follows the same identifier shape ${VAR}
# uses.
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
# A placeholder that is the *entire* scalar node (matched with fullmatch,
# not search) gets whole-value substitution: the parameter's full value,
# any type, replaces the string outright. Anything else containing a
# placeholder is embedded-text substitution instead, which requires a
# scalar parameter -- see _substitute_placeholders.
_WHOLE_PLACEHOLDER = re.compile(r"^\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")


def interpolate_env(
    value: Any,
    env: Mapping[str, str] | None = None,
    missing: set[str] | None = None,
) -> Any:
    """Replace ``${VAR}`` tokens in strings from ``env`` (default the process env).

    A referenced variable that is not set is a hard error, unless ``missing``
    is given: then the variable's name is added to it, the ``${VAR}`` token
    is left in place, and no exception is raised -- letting a caller collect
    every undefined variable across several calls before reporting them all
    at once.
    """
    environ = os.environ if env is None else env

    if isinstance(value, str):

        def replace(match: re.Match) -> str:
            name = match.group(1)
            if name not in environ:
                if missing is not None:
                    missing.add(name)
                    return match.group(0)
                raise ValueError(f"undefined environment variable: {name}")
            return environ[name]

        return _VAR.sub(replace, value)
    if isinstance(value, dict):
        return {
            key: interpolate_env(item, environ, missing)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [interpolate_env(item, environ, missing) for item in value]
    return value


@dataclass
class SourceConfig:
    name: str
    type: str
    params: dict
    timezone: str | None = None
    timeout: int | None = None


_DEFAULT_RETAIN_DAYS = 400


@dataclass
class StoreConfig:
    """Observation-store settings."""

    path: str | None = None
    retain_days: int = _DEFAULT_RETAIN_DAYS


def _parse_store(raw: Any) -> StoreConfig | None:
    """A bare string is shorthand for ``{path: ...}``; else a full mapping."""
    if raw is None:
        return None
    if isinstance(raw, str):
        return StoreConfig(path=raw)
    return StoreConfig(
        path=raw.get("path"),
        retain_days=raw.get("retain_days", _DEFAULT_RETAIN_DAYS),
    )


@dataclass
class Config:
    sources: dict[str, SourceConfig]
    checks: list[Check]
    config_dir: Path
    store: StoreConfig | None = None
    calendar: BusinessCalendar | None = None


class ConfigError(ValueError):
    """A config file could not be loaded, parsed, or validated.

    Raised by :func:`load_config` for every failure mode: a missing or
    unreadable file, a YAML parse error, a missing required field, an
    invalid expectation, or any validation problem (unknown source
    reference, duplicate check_id, calendar misuse, ...) -- always chained
    from the underlying cause via ``raise ... from exc``. Subclasses
    ``ValueError`` so callers that only care about "config problem" can
    keep catching ``ValueError``.
    """


@dataclass(frozen=True)
class ConfigProblem:
    """One problem found while validating a config, attributed to the
    file(s) it involves.

    ``files`` holds one entry for almost every problem -- the file a
    malformed check or an unresolved ``${VAR}`` came from. A duplicate
    ``check_id`` spanning two files is the one case with two entries, so
    a report grouped by file can list it under both.
    """

    files: tuple[Path, ...]
    message: str


@dataclass
class ConfigValidation:
    """The result of :func:`validate_config`: every problem found, plus
    the :class:`Config` it was still able to resolve.

    ``config`` is populated even when ``problems`` is non-empty -- a check
    that failed to build is simply left out of ``config.checks`` -- so a
    report can be built directly on top of what the config resolves to,
    without re-loading anything.
    """

    path: Path
    config: Config
    problems: list[ConfigProblem]

    @property
    def ok(self) -> bool:
        return not self.problems


def _parse_by_weekday(
    raw: Any, metric: str | None = None
) -> dict[str, Any] | None:
    if not raw:
        return None
    parsed = {}
    for day, expect in raw.items():
        if day not in WEEKDAY_NAMES:
            raise ValueError(f"unknown weekday in by_weekday: {day!r}")
        parsed[day] = parse_expectation(expect, metric=metric)
    return parsed


def _parse_check_calendar_mode(raw: Any) -> str | None:
    if raw is None:
        return None
    if raw not in _CHECK_CALENDAR_MODES:
        raise ValueError(f"unsupported check calendar mode: {raw!r}")
    return raw


def _resolve_skip_off_schedule(raw: dict, defaults: dict) -> bool:
    """``skip_off_schedule``, or its alias ``skip_on_holiday`` (spec 7.4).

    A check's own value (under either key name) wins over ``defaults:``
    (also under either key name); an explicit falsy value still counts as
    "own", so it correctly overrides a truthy default. Absent from both,
    the result is ``False``.
    """
    for mapping in (raw, defaults):
        if "skip_off_schedule" in mapping:
            return mapping["skip_off_schedule"]
        if "skip_on_holiday" in mapping:
            return mapping["skip_on_holiday"]
    return False


def _parse_freshness_source(raw: dict) -> str:
    """Return the ``freshness_source`` field verbatim; default ``column``.

    Meaningful only for ``metric: freshness``. Validation (an unrecognized
    name, a ``column`` origin missing its ``column:`` field, or a name the
    source's dialect doesn't support) happens in the accumulate-and-report
    pass (:func:`_validate_checks`) so a bad value here is reported
    alongside every other problem instead of aborting the load immediately.
    """
    return raw.get("freshness_source", "column")


def _describe_table_entry(entry: dict) -> str:
    """A short label for a ``tables:`` entry, used in problem messages.

    Mirrors :func:`_describe_raw_check`'s ``source.object`` shape: a
    table entry is identified by the same pair a check under it would be,
    since that pair is exactly what the entry states once on the checks'
    behalf.
    """
    return f"table {entry.get('source', '?')}.{entry.get('object', '?')}"


def _placeholder_names(value: Any) -> set[str]:
    """Every ``{{ name }}`` referenced anywhere under ``value``, whole-node
    or embedded alike -- the set's *full* placeholder set a with: key is
    checked against, deliberately gathered without regard to ``skip:``
    (see :func:`_expand_check_set`): a set-level default for a parameter
    used only by a skipped item must not become an error for every table
    that skips it.
    """
    if isinstance(value, str):
        return set(_PLACEHOLDER.findall(value))
    if isinstance(value, dict):
        names: set[str] = set()
        for item in value.values():
            names |= _placeholder_names(item)
        return names
    if isinstance(value, list):
        names = set()
        for item in value:
            names |= _placeholder_names(item)
        return names
    return set()


def _substitute_placeholders(value: Any, params: dict[str, Any]) -> Any:
    """Replace every ``{{ name }}`` under ``value`` with ``params[name]``.

    Two substitution rules, chosen per scalar string node: a placeholder
    that is the *entire* node (``_WHOLE_PLACEHOLDER`` fullmatches) is
    replaced by the parameter's value outright, type and all -- this is
    what lets ``expect: "{{ rows }}"`` carry a whole expectation mapping.
    A placeholder embedded in a longer string is replaced as text, which
    requires the parameter to be a scalar; a mapping or list there raises,
    since there is no sane way to embed one in a string.

    Raises ``ValueError`` -- caught and turned into a provenance-tagged
    problem string by the caller (:func:`_expand_check_set`), which is
    also the only place that knows which table/set/item to name.
    """
    if isinstance(value, str):
        whole = _WHOLE_PLACEHOLDER.match(value)
        if whole:
            name = whole.group(1)
            if name not in params:
                raise ValueError(name)
            return params[name]
        if not _PLACEHOLDER.search(value):
            return value

        def replace(match: re.Match) -> str:
            name = match.group(1)
            if name not in params:
                raise ValueError(name)
            param_value = params[name]
            if isinstance(param_value, dict | list):
                raise ValueError(f"{name}\x1fembedded")
            return str(param_value)

        return _PLACEHOLDER.sub(replace, value)
    if isinstance(value, dict):
        return {
            k: _substitute_placeholders(v, params) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_substitute_placeholders(v, params) for v in value]
    return value


def normalize_check_sets(
    raw: Any, file: Path
) -> tuple[dict[str, dict[str, Any]], list[ConfigProblem]]:
    """Validate and normalize one file's ``check_sets:`` mapping into
    ``{name: {"with": dict, "checks": list}}``.

    A malformed set -- not a mapping, an unknown key, a missing or
    non-list ``checks:``, a non-dict ``with:`` -- is reported as a
    :class:`ConfigProblem` and left out of the returned dict entirely: a
    set that failed to normalize can't be expanded into anything
    downstream needs to see, mirroring how :func:`flatten_table_checks`
    handles a malformed ``tables:`` entry.

    Tolerant by design, not just by convention: :func:`dbfresh.configurator._raw_checks_in`
    reuses this for cross-file dedup scanning, where a broken
    ``check_sets:`` entry is ``config validate``'s problem to report, not
    this pass's -- discarding the returned problems there and keeping only
    the sets that did normalize is exactly the same tolerance
    ``_raw_checks_in`` already applies to a malformed ``tables:`` entry.
    """
    if raw is None:
        return {}, []
    if not isinstance(raw, dict):
        return {}, [
            ConfigProblem(
                files=(file,),
                message=f"check_sets: must be a mapping, got {raw!r}",
            )
        ]

    sets: dict[str, dict[str, Any]] = {}
    problems: list[ConfigProblem] = []
    for name, definition in raw.items():
        label = f"check_sets {name!r}"
        if not isinstance(definition, dict):
            problems.append(
                ConfigProblem(
                    files=(file,),
                    message=f"{label}: must be a mapping, got {definition!r}",
                )
            )
            continue
        extra_keys = sorted(set(definition) - _CHECK_SET_KEYS)
        if extra_keys:
            problems.append(
                ConfigProblem(
                    files=(file,),
                    message=f"{label}: unknown check_sets field(s): {extra_keys}",
                )
            )
        checks = definition.get("checks")
        if not isinstance(checks, list):
            problems.append(
                ConfigProblem(
                    files=(file,),
                    message=f"{label}: 'checks' is required and must be a list",
                )
            )
            continue
        with_defaults = definition.get("with")
        if with_defaults is None:
            with_defaults = {}
        elif not isinstance(with_defaults, dict):
            problems.append(
                ConfigProblem(
                    files=(file,),
                    message=f"{label}: 'with' must be a mapping, got {with_defaults!r}",
                )
            )
            continue
        sets[name] = {"with": with_defaults, "checks": checks}
    return sets, problems


def _expand_check_set(
    label: str,
    entry: dict,
    check_sets: Mapping[str, dict[str, Any]],
) -> tuple[list[dict], list[str]]:
    """Expand one ``tables:`` entry's ``use:``/``with:``/``skip:`` into raw
    check dicts, carrying the entry's ``source``/``object`` exactly like
    every other block :func:`flatten_table_checks` returns.

    Every problem found here is prefixed with ``label`` and, once the set
    itself is known, the set's name -- "table dbo.fct_sales, set
    'standard', item 3" -- so a problem with an expanded check is
    traceable even though it appears verbatim in no file.
    """
    set_name = entry.get("use")
    if not isinstance(set_name, str):
        return [], [f"{label}: 'use' must be a string, got {set_name!r}"]

    set_def = check_sets.get(set_name)
    if set_def is None:
        return [], [
            f"{label}: use: references unknown check_sets entry {set_name!r}"
        ]

    table_with = entry.get("with")
    if table_with is not None and not isinstance(table_with, dict):
        return [], [f"{label}: 'with' must be a mapping, got {table_with!r}"]
    table_with = table_with or {}

    table_skip = entry.get("skip")
    if table_skip is not None and not isinstance(table_skip, list):
        return [], [f"{label}: 'skip' must be a list, got {table_skip!r}"]
    skip = set(table_skip or [])

    problems: list[str] = []
    set_label = f"{label}, set {set_name!r}"

    set_checks = set_def["checks"]
    full_placeholders = _placeholder_names(set_checks)
    merged_params = {**set_def["with"], **table_with}
    for key in sorted(set(merged_params) - full_placeholders):
        problems.append(
            f"{set_label}: with: key {key!r} matches no placeholder in "
            "this set"
        )

    set_metrics = {
        item.get("metric")
        for item in set_checks
        if isinstance(item, dict) and item.get("metric") is not None
    }
    for name in sorted(skip - set_metrics):
        problems.append(
            f"{set_label}: skip: {name!r} is not a metric defined in this set"
        )

    expanded: list[dict] = []
    for item_index, item in enumerate(set_checks):
        if not isinstance(item, dict):
            problems.append(
                f"{set_label} item {item_index}: check block must be a "
                f"mapping, got {item!r}"
            )
            continue
        metric = item.get("metric")
        if metric is not None and metric in skip:
            continue
        restated = sorted(set(item) & _TABLE_CHECK_OWN_FIELDS)
        if restated:
            problems.append(
                f"{set_label} item {item_index}: declares its own "
                f"{restated}; the table already sets it for every check "
                "expanded from the set"
            )
            continue
        item_label = f"{set_label} item {item_index}"
        try:
            substituted = _substitute_placeholders(item, merged_params)
        except ValueError as exc:
            [detail] = exc.args
            if "\x1f" in detail:
                name, _ = detail.split("\x1f", 1)
                param_value = merged_params[name]
                problems.append(
                    f"{item_label}: parameter {name!r} is a "
                    f"{type(param_value).__name__}; a value embedded in a "
                    "longer string must be a scalar"
                )
            else:
                problems.append(
                    f"{item_label}: parameter {detail!r} has no value -- "
                    f"not supplied by with: on set {set_name!r} or by "
                    f"{label}'s own with:"
                )
            continue
        block = dict(substituted)
        block["source"] = entry["source"]
        block["object"] = entry["object"]
        expanded.append(block)

    return expanded, problems


def flatten_table_checks(
    tables: list[Any],
    check_sets: Mapping[str, dict[str, Any]] | None = None,
) -> tuple[list[dict], list[str]]:
    """Flatten ``tables:`` entries into raw check dicts carrying the
    entry's ``source``/``object`` -- the raw-dict pass that runs before
    :func:`_build_check` ever sees a check. Once flattened, a check that
    came from ``tables:`` is a plain dict indistinguishable from one
    written directly under a flat ``checks:`` list, so everything
    downstream (``defaults:`` merging, expectation parsing, unknown-field
    validation, calendar validation, ``check_id`` derivation, duplicate
    detection) runs unchanged over the result -- this function is the
    only place that needs to know ``tables:`` exists at all.

    Deliberately a pure function, unaware of ``_load_config``'s
    ``collect_all_errors`` switch: it always flattens everything it can
    and returns every problem found as plain text, leaving "raise on the
    first" vs. "collect all, attributed to a file" entirely to the
    caller. That is also what lets
    :func:`dbfresh.configurator._raw_checks_in` reuse this for dedup key
    computation, where a malformed entry is not this function's problem
    to report -- ``config validate`` already owns that -- so it just
    flattens what it can and ignores the rest.

    Returns the raw check dicts in document order (entries in the order
    given, each entry's checks in their own order) and the problem
    strings in the order found. A problem entry contributes no checks; a
    problem check contributes no dict but does not block its siblings.

    ``check_sets`` is the composed ``{name: {with, checks}}`` mapping a
    ``use:`` entry expands against (see :func:`normalize_check_sets` and
    :func:`_expand_check_set`); omit it -- or pass an entry with no
    ``use:`` -- and a table entry behaves exactly as it did before
    check_sets existed. An entry's expanded-from-set checks come before
    its own nested ``checks:``, so a table's inline checks always read as
    "on top of the battery" rather than interleaved with it.
    """
    check_sets = check_sets or {}
    checks: list[dict] = []
    problems: list[str] = []

    for index, entry in enumerate(tables):
        if not isinstance(entry, dict):
            problems.append(
                f"tables: entry {index} must be a mapping, got {entry!r}"
            )
            continue

        label = _describe_table_entry(entry)
        extra_keys = sorted(set(entry) - _TABLE_ENTRY_KEYS)
        if extra_keys:
            problems.append(f"{label}: unknown table field(s): {extra_keys}")

        # Reported here, once, rather than left to _build_check. The
        # entry states source and object on behalf of every check under
        # it, so omitting one is a single table-level mistake -- letting
        # it through would raise the same "missing required field" once
        # per nested check, none of the copies naming the table that
        # actually omitted it.
        missing_fields = sorted(_TABLE_CHECK_OWN_FIELDS - set(entry))
        if missing_fields:
            problems.append(
                f"{label}: table entry must set {missing_fields}; "
                "it states them once for every check nested under it"
            )
            continue

        if "use" in entry:
            set_checks, set_problems = _expand_check_set(
                label, entry, check_sets
            )
            checks.extend(set_checks)
            problems.extend(set_problems)
        elif "with" in entry or "skip" in entry:
            problems.append(
                f"{label}: 'with'/'skip' require 'use' -- they parameterize "
                "and filter a named check set, and mean nothing without one"
            )
            continue

        nested_checks = entry.get("checks")
        if nested_checks is None:
            nested_checks = []
        elif not isinstance(nested_checks, list):
            problems.append(
                f"{label}: 'checks' must be a list, got {nested_checks!r}"
            )
            nested_checks = []

        for nested in nested_checks:
            if not isinstance(nested, dict):
                problems.append(
                    f"{label}: check block must be a mapping, got {nested!r}"
                )
                continue
            restated = sorted(set(nested) & _TABLE_CHECK_OWN_FIELDS)
            if restated:
                problems.append(
                    f"{label}: nested check declares its own {restated}; "
                    "the table already sets it for every check under it"
                )
                continue
            expanded = dict(nested)
            if "source" in entry:
                expanded["source"] = entry["source"]
            if "object" in entry:
                expanded["object"] = entry["object"]
            checks.append(expanded)

    return checks, problems


def group_checks_by_table(checks: list[dict]) -> list[dict]:
    """The inverse of :func:`flatten_table_checks`: group raw check dicts
    -- each still carrying its own ``source``/``object`` -- into
    ``tables:`` entries, one per distinct pair.

    Entries come out in the order their pair first appears in ``checks``;
    a pair's own checks keep their relative order from ``checks`` too, so
    a list built from a mix of origins (a flat ``checks:`` list and
    already-flattened ``tables:`` entries) folds into one stable,
    predictable ``tables:`` block regardless of where each check
    originated -- which is what lets a partially-migrated file converge
    on one entry per pair instead of two. ``source``/``object`` move up
    onto the entry and are dropped from the nested block, matching what a
    hand-written ``tables:`` entry looks like.

    The sole caller is ``dbfresh config migrate``, the one place that
    builds ``tables:`` entries rather than consuming them.
    """
    order: list[tuple[Any, Any]] = []
    grouped: dict[tuple[Any, Any], list[dict]] = {}
    for raw in checks:
        pair = (raw.get("source"), raw.get("object"))
        if pair not in grouped:
            grouped[pair] = []
            order.append(pair)
        grouped[pair].append(
            {k: v for k, v in raw.items() if k not in _TABLE_CHECK_OWN_FIELDS}
        )
    return [
        {"source": source, "object": obj, "checks": grouped[(source, obj)]}
        for source, obj in order
    ]


def _build_check(raw: dict, defaults: dict) -> Check:
    """Build one Check, merging ``defaults:`` fields the check itself omits.

    Merged fields are ``severity``, ``calendar``, ``where``, ``allow_empty``,
    and ``skip_off_schedule``; a per-check value always overrides the
    default, including an explicit falsy value such as ``allow_empty: false``.
    """
    metric = raw.get("metric")
    expect = (
        parse_expectation(raw["expect"], metric=metric)
        if "expect" in raw
        else None
    )
    on_holiday = raw.get("on_holiday")
    return Check(
        source=raw["source"],
        object=raw["object"],
        metric=metric,
        column=raw.get("column"),
        key=raw.get("key"),
        where=raw.get("where", defaults.get("where")),
        assert_=raw.get("assert"),
        assert_sql=raw.get("assert_sql"),
        expect=expect,
        allow_empty=raw.get("allow_empty", defaults.get("allow_empty", False)),
        severity=raw.get("severity", defaults.get("severity", "error")),
        id=raw.get("id"),
        by_weekday=_parse_by_weekday(raw.get("by_weekday"), metric=metric),
        on_holiday=(
            parse_expectation(on_holiday, metric=metric)
            if on_holiday
            else None
        ),
        calendar=_parse_check_calendar_mode(
            raw.get("calendar", defaults.get("calendar"))
        ),
        skip_off_schedule=_resolve_skip_off_schedule(raw, defaults),
        freshness_source=_parse_freshness_source(raw),
    )


def _describe_raw_check(raw: Any) -> str:
    """A best-effort check label for a raw block that failed to even
    build into a :class:`Check` -- :func:`~dbfresh.checks.describe_check`
    needs a built ``Check``, which doesn't exist yet for one of these, so
    this falls back to whatever of ``source``/``object`` the raw block
    does have.
    """
    if not isinstance(raw, dict):
        return "check"
    return f"{raw.get('source', '?')}.{raw.get('object', '?')}"


def _config_error_text(exc: Exception) -> str:
    """Translate a check-build exception into message text.

    Shared by :func:`_load_config_or_raise` (the single-error path) and
    :func:`_load_config`'s collecting path, so the two never drift on
    wording for the same underlying exception.
    """
    if isinstance(exc, KeyError):
        return f"missing required field: {exc}"
    if isinstance(exc, TypeError):
        return f"invalid expectation: {exc}"
    return str(exc)


def resolve_includes(config_dir: Path, patterns: Any) -> list[Path]:
    """Resolve root-only ``include:`` globs to matched files.

    Each glob is relative to ``config_dir`` — the root config's directory,
    never the process CWD. A glob matching no files is a validation
    error (a mistyped include must not silently drop checks). Matched files
    across all globs are deduplicated and returned in lexicographic path
    order; the load order itself carries no semantics.

    Shared with :func:`dbfresh.configurator.target_files` so both the
    loader and the wizard/TUI resolve ``include:`` identically -- an
    unmatched glob is a hard error in both, never a silently empty list.
    """
    if not isinstance(patterns, list):
        raise ValueError("'include' must be a list of path globs")

    matched: set[Path] = set()
    for pattern in patterns:
        found = [p for p in config_dir.glob(pattern) if p.is_file()]
        if not found:
            raise ValueError(f"include glob matched no files: {pattern!r}")
        matched.update(found)

    return sorted(matched, key=lambda p: p.as_posix())


_INCLUDED_FILE_ALLOWED_KEYS = frozenset({"checks", "tables", "check_sets"})


def _load_included_checks(
    raw: Any, path: Path
) -> tuple[list[dict], list[Any], Any]:
    """Normalize an included file's parsed YAML into its flat ``checks:``
    list, its ``tables:`` list, and its own ``check_sets:`` mapping (or
    ``None``), all unexpanded/unnormalized.

    An included file contributes checks two ways: a bare sequence of
    check blocks (equivalent to ``checks:`` alone, and never carries
    ``check_sets:``), or a mapping with ``checks:``/``tables:``/
    ``check_sets:``. ``include:``, ``sources:``, ``calendar:``,
    ``store:``, and ``defaults:`` may appear only in the root config, so
    any other top-level key here is a validation error. A team shares one
    standards file this way: a ``check_sets:``-only included file, used
    by ``tables:`` entries anywhere in the composed config.

    Expansion of ``tables:`` (which needs every file's ``check_sets:``
    composed first -- see :func:`_load_config`) happens later, uniformly
    for the root config and every included file, in
    :func:`_collect_file_checks` -- this function only validates the
    file's shape and hands back what it declared.
    """
    if raw is None:
        return [], [], None
    if isinstance(raw, list):
        return raw, [], None
    if isinstance(raw, dict):
        extra = sorted(set(raw) - _INCLUDED_FILE_ALLOWED_KEYS)
        if extra:
            raise ValueError(
                f"included file {path} may only declare top-level "
                "'checks:', 'tables:', and 'check_sets:' keys; found "
                f"disallowed key(s): {extra}"
            )
        return (
            list(raw.get("checks") or []),
            list(raw.get("tables") or []),
            raw.get("check_sets"),
        )
    raise ValueError(
        f"included file {path} must be a checks list or a "
        "{checks: [...], tables: [...], check_sets: {...}} mapping"
    )


def _read_included_file(
    path: Path, env: dict[str, str] | None, missing: set[str] | None = None
) -> tuple[list[dict], list[Any], Any]:
    raw = yaml.safe_load(path.read_text())
    raw = interpolate_env(raw, env, missing)
    return _load_included_checks(raw, path)


def _collect_file_checks(
    flat: list[dict],
    tables: list[Any],
    file: Path,
    collect_all_errors: bool,
    check_sets: Mapping[str, dict[str, Any]] | None = None,
) -> tuple[list[dict], list[Path], list[ConfigProblem]]:
    """One file's raw checks: the flat ``checks:`` list first, then
    ``tables:`` entries flattened (via :func:`flatten_table_checks`) in
    document order -- the deterministic per-file ordering
    :func:`_load_config` composes across files, root first then each
    included file in turn. Every check returned here is attributed to
    ``file``, whichever list it came from.

    ``check_sets`` is the mapping composed across every file in the
    config (root plus every included file) -- a ``use:`` reference is
    resolved against all of it, not just this file's own, since a table
    in one file may use a set defined in another.

    Never raises itself, even when ``collect_all_errors`` is false: this
    only assembles the raw check list, before ``_load_config`` has even
    finished resolving every included file and checked for an undefined
    ``${VAR}`` -- raising here would report a ``tables:`` problem ahead
    of an undefined-variable problem that, by convention, is always
    surfaced first. ``_load_config`` raises on the first collected
    problem itself, once that point is reached, when not collecting.
    """
    table_checks, table_problem_texts = flatten_table_checks(
        tables, check_sets
    )
    problems = [
        ConfigProblem(files=(file,), message=text)
        for text in table_problem_texts
    ]
    raw = [*flat, *table_checks]
    return raw, [file] * len(raw), problems


def _load_config_or_raise(
    path: str | Path,
    env: dict[str, str] | None,
    collect_missing: bool,
    collect_all_errors: bool = False,
) -> tuple[Config, frozenset[str], list[ConfigProblem]]:
    """Shared exception-translation boundary for :func:`load_config`,
    :func:`load_config_tolerant`, and :func:`validate_config` -- all
    three call :func:`_load_config` and turn every failure mode into a
    single :class:`ConfigError`, chained from its underlying cause: a
    missing or unreadable file, a YAML parse error, a missing required
    field, an invalid expectation, or any of the validation checks in
    :func:`_validate_checks` / :func:`_validate_sources`.

    Only :func:`validate_config` passes ``collect_all_errors=True``; even
    then, a problem that blocks resolving the check set at all (bad YAML,
    an unmatched ``include:`` glob, ...) still raises here rather than
    being collected -- see :func:`_load_config`.
    """
    try:
        return _load_config(
            path,
            env,
            collect_missing=collect_missing,
            collect_all_errors=collect_all_errors,
        )
    except ConfigError:
        raise
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(_config_error_text(exc)) from exc


def load_config(path: str | Path, env: dict[str, str] | None = None) -> Config:
    """Parse a YAML config, interpolate secrets, and validate references.

    Supports composition: the root config's ``include:`` list of
    path globs, resolved relative to the root config's directory, whose
    matched files each contribute a ``checks:`` list merged with the root
    file's own. The composed check list is validated as one unit, so a
    duplicate ``check_id`` anywhere across the root and included files is a
    validation error.

    Every load, parse, or validation failure surfaces as a single
    :class:`ConfigError`, chained from its underlying cause -- a missing
    or unreadable file, a YAML parse error, a missing required field, an
    invalid expectation, or any of the validation checks below, including
    an undefined ``${VAR}`` reference. See :func:`load_config_tolerant`
    for the one caller (``dbfresh ui``) that needs an undefined variable
    to not be fatal.
    """
    config, _missing, _problems = _load_config_or_raise(
        path, env, collect_missing=False
    )
    return config


def load_config_tolerant(
    path: str | Path, env: dict[str, str] | None = None
) -> tuple[Config, frozenset[str]]:
    """Like :func:`load_config`, but an undefined ``${VAR}`` reference is
    collected into the returned set instead of raising -- the affected
    parameter keeps its literal ``${VAR}`` token rather than being
    resolved. Every other failure mode (a missing or unreadable file, a
    YAML parse error, a missing required field, an invalid expectation, or
    any validation problem such as an unknown source reference or a
    duplicate check_id) still raises :class:`ConfigError` exactly as
    :func:`load_config` does -- this widens only the undefined-variable
    case, nothing else.

    Meant for ``dbfresh ui`` only: a shared config repo whose secrets
    nobody has set yet is a normal first run, not a broken config, so the
    TUI can launch and show what's missing instead of refusing to start.
    Every other config-reading command (``run``/``history``/``prune``/
    ``add``) keeps calling :func:`load_config` and still hard-errors on a
    missing secret, since a CLI run against an unresolved secret should
    fail clearly rather than silently query the wrong thing.
    """
    config, missing, _problems = _load_config_or_raise(
        path, env, collect_missing=True
    )
    return config, missing


def validate_config(
    path: str | Path, env: dict[str, str] | None = None
) -> ConfigValidation:
    """Load ``path`` exactly as :func:`load_config` does, but collect
    every check-build and validation problem instead of stopping at the
    first -- the engine behind ``dbfresh config validate``.

    Every malformed check, unknown source reference, duplicate
    ``check_id``, and undefined ``${VAR}`` reference is collected and
    attributed to the file it came from, via :func:`_load_config`'s
    ``collect_all_errors`` path. A problem that blocks resolving the
    check set entirely -- a missing or unreadable file, invalid YAML, or
    an ``include:`` glob matching no files -- cannot be collected past
    and still raises :class:`ConfigError`, exactly as :func:`load_config`
    does.
    """
    config, _missing, problems = _load_config_or_raise(
        path, env, collect_missing=True, collect_all_errors=True
    )
    return ConfigValidation(path=Path(path), config=config, problems=problems)


def collect_referenced_env_vars(path: str | Path) -> list[str]:
    """Return the sorted, deduplicated names of every ``${VAR}`` the root
    config and its resolved includes reference.

    Interpolates against an empty environment, not ``os.environ``: passing
    ``{}`` to :func:`interpolate_env` means every reference is undefined,
    so every one of them lands in ``missing`` regardless of whether it
    happens to be set on the machine generating the template. Building the
    template from the real environment would silently omit a variable
    that's already set there -- exactly the one a colleague sharing the
    config still needs to be told about.

    Does not build or validate a full :class:`Config`: no source-parameter,
    metric, or severity checks run, and an undefined variable is never an
    error here. Value-level validation must not block template generation,
    and listing an unset var is the entire point -- this is meant to run
    before secrets exist.

    Known limit: a ``${VAR}`` referenced only inside a file reachable
    through an include glob pattern that itself contains an unresolved
    ``${VAR}`` is not collected, because that pattern is never resolved --
    identical to :func:`_load_config`'s own behavior. The variable name
    appearing in the include pattern itself is still collected; only names
    unique to the unreached included file are missed.
    """
    path = Path(path)
    missing: set[str] = set()
    try:
        data = yaml.safe_load(path.read_text()) or {}
        data = interpolate_env(data, {}, missing)

        include_patterns = data.get("include")
        if isinstance(include_patterns, list):
            patterns = [
                pattern
                for pattern in include_patterns
                if not (isinstance(pattern, str) and _VAR.search(pattern))
            ]
            config_dir = path.resolve().parent
            for include_path in resolve_includes(config_dir, patterns):
                _read_included_file(include_path, {}, missing)
    except ConfigError:
        raise
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    return sorted(missing)


def _validate_metric_fields(check: Check, label: str) -> list[ValueError]:
    """Discriminating-field and expectation checks for a known metric.

    Only called once ``check.metric`` is confirmed to be a registered
    metric name -- an unknown metric is reported on its own, without also
    complaining about a discriminating field it can't even look up.
    """
    assert check.metric is not None
    errors: list[ValueError] = []
    required = _METRIC_REQUIRED.get(check.metric)
    # freshness's "column" requirement is conditional on freshness_source,
    # so it is validated separately in _validate_freshness_source.
    if (
        required == "column"
        and check.metric != "freshness"
        and not check.column
    ):
        errors.append(
            ValueError(f"{label}: metric {check.metric!r} requires 'column'")
        )
    if required == "key" and not check.key:
        errors.append(
            ValueError(f"{label}: metric {check.metric!r} requires 'key'")
        )
    if check.expect is None:
        errors.append(
            ValueError(f"{label}: metric check has no expectation (expect:)")
        )
    return errors


def _validate_freshness_source(
    check: Check, sources: dict[str, SourceConfig], label: str
) -> list[ValueError]:
    """Validate ``freshness_source``: the ``column`` origin needs a column;
    the two DESCRIBE origins need dialect capability."""
    if check.metric != "freshness":
        return []
    if check.freshness_source == "column":
        if not check.column:
            return [
                ValueError(
                    f"{label}: freshness_source 'column' requires 'column'"
                )
            ]
        return []
    try:
        dialect = dialect_for_type(sources[check.source].type)
    except ValueError:
        return []  # an unknown source type is a connect-time concern, not this pass's
    try:
        validate_freshness_source(check.freshness_source, dialect)
    except ValueError as exc:
        return [ValueError(f"{label}: {exc}")]
    return []


def _validate_checks(
    raw_checks: list[dict],
    checks: list[Check],
    sources: dict[str, SourceConfig],
    calendar: BusinessCalendar | None,
) -> list[tuple[tuple[int, ...], ValueError]]:
    """Collect every check-level validation problem instead of raising on
    the first one found.

    Covers: unknown source references, unknown metrics, missing
    discriminating fields, a metric check with no expectation, a check with
    none of metric/assert/assert_sql (or more than one of them -- exactly
    one primitive is required), unknown check-block keys, an invalid
    ``severity``, ``max_lag`` used outside ``freshness``, freshness-source
    problems (missing column, dialect capability), duplicate ``check_id``s,
    and calendar features used without a top-level ``calendar:`` block.

    Each error is paired with the index (into ``checks``) of the check
    responsible for it -- both indices, for a duplicate ``check_id``, since
    two checks collide. :func:`_load_config`'s collecting path uses this
    to attribute a problem to the file the offending check came from; the
    raising path discards the tag and raises the bare errors, in the same
    order, exactly as before this tagging existed.
    """
    errors: list[tuple[tuple[int, ...], ValueError]] = []
    seen: dict[str, tuple[int, Check]] = {}
    metric_names = {spec.name for spec in METRICS}

    for i, (raw, check) in enumerate(zip(raw_checks, checks, strict=True)):
        label = describe_check(check)
        check_errors: list[ValueError] = []

        extra_keys = sorted(set(raw) - _CHECK_KEYS)
        if extra_keys:
            check_errors.append(
                ValueError(f"{label}: unknown check field(s): {extra_keys}")
            )

        primitives = [
            name
            for name, present in (
                ("metric", check.metric is not None),
                ("assert", check.assert_ is not None),
                ("assert_sql", check.assert_sql is not None),
            )
            if present
        ]
        if not primitives:
            check_errors.append(
                ValueError(
                    f"{label}: check has none of metric, assert, or assert_sql"
                )
            )
        elif len(primitives) > 1:
            check_errors.append(
                ValueError(
                    f"{label}: check has more than one of metric/assert/assert_sql "
                    f"({', '.join(primitives)}) -- a check must set exactly one"
                )
            )

        if check.severity not in _VALID_SEVERITIES:
            check_errors.append(
                ValueError(
                    f"{label}: severity must be 'error' or 'warn', "
                    f"got {check.severity!r}"
                )
            )

        if (
            check.expect is not None
            and check.expect.operator == "max_lag"
            and check.metric != "freshness"
        ):
            check_errors.append(
                ValueError(
                    f"{label}: 'max_lag' is only valid for the freshness metric"
                )
            )

        if check.source not in sources:
            check_errors.append(
                ValueError(
                    f"check references unknown source: {check.source!r}"
                )
            )
        elif check.metric is not None and check.metric not in metric_names:
            check_errors.append(
                ValueError(f"{label}: unknown metric: {check.metric!r}")
            )
        elif check.metric is not None:
            check_errors.extend(_validate_metric_fields(check, label))
            check_errors.extend(
                _validate_freshness_source(check, sources, label)
            )

        if not calendar and (
            check.by_weekday
            or check.on_holiday is not None
            or check.calendar == "business"
            or check.skip_off_schedule
        ):
            check_errors.append(
                ValueError(
                    f"check on {check.object!r} uses calendar features "
                    "(by_weekday/on_holiday/calendar/skip_off_schedule) but no "
                    "top-level calendar: block is configured"
                )
            )

        errors.extend(((i,), error) for error in check_errors)

        cid = check_id(check)
        if cid in seen:
            seen_index, seen_check = seen[cid]
            errors.append(
                (
                    (seen_index, i),
                    ValueError(
                        f"duplicate check_id {cid!r}: {describe_check(seen_check)} "
                        f"and {label} collide -- add an explicit id: to "
                        "one of them to disambiguate"
                    ),
                )
            )
        else:
            seen[cid] = (i, check)

    return errors


def _validate_databricks_auth(name: str, params: dict) -> list[ValueError]:
    """Auth-param coherence for a databricks source, at config-load time.

    auth_type selects the method: 'pat' (the default) uses token;
    'oauth_m2m' uses a service principal's client_id and client_secret.
    This checks the *combination*, which name-based signature validation
    can't -- so a contradictory or half-specified config fails at load
    rather than as a confusing connect-time error, and a leftover token
    can't silently shadow an intended service-principal login.
    """
    auth_type = params.get("auth_type")
    if auth_type not in (None, "pat", "oauth_m2m"):
        return [
            ValueError(
                f"source {name!r}: auth_type must be 'pat' or "
                f"'oauth_m2m', got {auth_type!r}"
            )
        ]
    has_token = "token" in params
    has_id = "client_id" in params
    has_secret = "client_secret" in params
    errors: list[ValueError] = []
    if auth_type == "oauth_m2m":
        if not (has_id and has_secret):
            errors.append(
                ValueError(
                    f"source {name!r}: auth_type: oauth_m2m requires "
                    "both client_id and client_secret"
                )
            )
        if has_token:
            errors.append(
                ValueError(
                    f"source {name!r}: auth_type: oauth_m2m does not "
                    "use token -- remove it"
                )
            )
    else:  # pat (explicit or default)
        if not has_token:
            errors.append(
                ValueError(
                    f"source {name!r}: a databricks source needs "
                    "token, or auth_type: oauth_m2m with client_id "
                    "and client_secret"
                )
            )
        if has_id or has_secret:
            errors.append(
                ValueError(
                    f"source {name!r}: client_id/client_secret "
                    "require auth_type: oauth_m2m"
                )
            )
    return errors


def _validate_sources(sources: dict[str, SourceConfig]) -> list[ValueError]:
    """Reject a genuinely-unknown source parameter with a clean error.

    Introspects the adapter class's ``__init__`` parameters via the
    factory (:func:`~dbfresh.adapters.factory.adapter_class_for`) without
    constructing or connecting it. A source whose ``type:`` isn't a
    registered adapter is skipped here -- that is a connect-time concern
    (``create_adapter`` already raises there, turned into a per-check
    ``ERROR`` result, see ``runner.run_and_persist``), not a config
    validation failure: an unreferenced or intentionally-unreachable
    source must not block a load that never touches it.

    Also probes each source's optional ``timezone:`` via
    :class:`zoneinfo.ZoneInfo` -- an invalid name would otherwise load
    cleanly and only surface as a per-check ``ERROR`` at run time, the
    first time a freshness check on that source converts a naive
    timestamp.
    """
    errors: list[ValueError] = []
    for name, source in sources.items():
        if source.timezone is not None:
            try:
                ZoneInfo(source.timezone)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                errors.append(
                    ValueError(
                        f"source {name!r}: invalid timezone {source.timezone!r}: {exc}"
                    )
                )
        try:
            cls = adapter_class_for(source.type)
        except ValueError:
            continue
        # inspect.signature(cls), not cls.__init__: the constructor
        # signature already excludes ``self``.
        valid_params = set(inspect.signature(cls).parameters)
        unknown = sorted(set(source.params) - valid_params)
        if unknown:
            errors.append(
                ValueError(
                    f"source {name!r} ({source.type}): unknown parameter(s) "
                    f"{unknown}; expected one of {sorted(valid_params)}"
                )
            )
        if source.type == "databricks":
            errors.extend(_validate_databricks_auth(name, source.params))
    return errors


def _raise_validation_errors(errors: list[ValueError]) -> None:
    """Raise the sole error verbatim; several are joined into one summary.

    A single problem's message is exactly that problem's text (matching
    the pre-existing single-error behavior); several problems are numbered
    and joined so every one of them is visible in the one raised error.
    """
    if len(errors) == 1:
        raise errors[0]
    summary = "\n".join(f"- {error}" for error in errors)
    raise ValueError(
        f"{len(errors)} configuration problems found:\n{summary}"
    ) from errors[0]


def _load_config(
    path: str | Path,
    env: dict[str, str] | None = None,
    collect_missing: bool = False,
    collect_all_errors: bool = False,
) -> tuple[Config, frozenset[str], list[ConfigProblem]]:
    """``collect_missing`` mirrors :func:`interpolate_env`'s own ``missing``
    parameter: false (the default, used by :func:`load_config`) raises on
    any undefined ``${VAR}``; true (used by :func:`load_config_tolerant`
    and :func:`validate_config`) collects every undefined name into the
    returned set instead, leaving the ``${VAR}`` token literal in place
    rather than resolving it.

    ``collect_all_errors`` (only ``True`` for :func:`validate_config`)
    changes how a broken check list is handled: a check that fails to
    build is skipped and recorded as a :class:`ConfigProblem` instead of
    raising immediately, and the :func:`_validate_sources` /
    :func:`_validate_checks` problems are likewise collected and returned
    rather than raised via :func:`_raise_validation_errors`. Everything
    upstream of the check list -- YAML parsing, ``include:`` resolution --
    is unaffected and still raises: a config whose check set can't even be
    determined has nothing to collect past that point. The returned
    ``list[ConfigProblem]`` is always empty when ``collect_all_errors`` is
    false, since any problem would already have raised by the time this
    function returns.
    """
    path = Path(path)
    config_dir = path.resolve().parent
    data = yaml.safe_load(path.read_text()) or {}

    # Each file's undefined ${VAR} names are collected into their own set
    # (not one set shared across the root and every included file) so the
    # collecting path can attribute each one to the file that referenced
    # it; `missing` below re-merges them into the single flat set the
    # raising path has always used.
    missing_by_file: dict[Path, set[str]] = {}
    root_missing: set[str] = set()
    data = interpolate_env(data, env, root_missing)
    missing_by_file[path] = root_missing

    # `problems` starts here rather than at the check-build loop below: a
    # `tables:` entry can be malformed (an unknown table field, a nested
    # check restating `source`/`object`) before a single Check is ever
    # built from it, and `collect_all_errors` must see those problems too.
    problems: list[ConfigProblem] = []

    # Every file is read before any `tables:` is flattened -- root first,
    # then each included file in resolved order -- because a table in one
    # file may `use:` a check_sets: entry defined in another. Flattening
    # file-by-file, as this used to, could never see across that boundary:
    # by the time an included file's tables were flattened, a check_sets:
    # entry the *root* declares later in this same function wouldn't exist
    # yet, and the reverse (a table in the root using a set an included
    # file declares) never worked at all. `file_entries` holds each file's
    # unexpanded pieces; check_sets: composition and `tables:` flattening
    # both run as a second pass below, once every file has been read.
    file_entries: list[tuple[Path, list[dict], list[Any], Any]] = [
        (
            path,
            list(data.get("checks") or []),
            list(data.get("tables") or []),
            data.get("check_sets"),
        )
    ]

    include_patterns = data.get("include")
    if include_patterns:
        if isinstance(include_patterns, list):
            # A pattern still containing an unresolved ${VAR} token (its
            # variable was undefined, so interpolate_env left it in place
            # and recorded it in `missing`) is never glob-resolved -- doing
            # so would only ever match zero files and misreport the
            # problem as an unmatched glob instead of the undefined
            # variable it actually is.
            include_patterns = [
                pattern
                for pattern in include_patterns
                if not (isinstance(pattern, str) and _VAR.search(pattern))
            ]
        for include_path in resolve_includes(config_dir, include_patterns):
            file_missing: set[str] = set()
            inc_flat, inc_tables, inc_check_sets = _read_included_file(
                include_path, env, file_missing
            )
            missing_by_file[include_path] = file_missing
            file_entries.append(
                (include_path, inc_flat, inc_tables, inc_check_sets)
            )

    missing: set[str] = set().union(*missing_by_file.values())

    if missing and not collect_missing:
        names = ", ".join(sorted(missing))
        raise ConfigError(
            f"undefined environment variable: {names}"
            if len(missing) == 1
            else f"undefined environment variables: {names}"
        )

    # check_sets: composed across every file, root first then included
    # files in resolved order -- the same order file_entries already
    # holds them in. A name defined in more than one file is reported
    # rather than letting whichever file happens to be read last silently
    # win; the first file to define a name keeps it, and normalize_check_sets's
    # own per-file problems (an unknown key, a missing checks:, ...) are
    # collected right alongside.
    check_sets: dict[str, dict[str, Any]] = {}
    check_sets_file: dict[str, Path] = {}
    for file, _flat, _tables, raw_check_sets in file_entries:
        normalized, cs_problems = normalize_check_sets(raw_check_sets, file)
        problems.extend(cs_problems)
        for name, definition in normalized.items():
            if name in check_sets:
                problems.append(
                    ConfigProblem(
                        files=(check_sets_file[name], file),
                        message=(
                            f"check_sets {name!r} is defined in more than "
                            "one file"
                        ),
                    )
                )
                continue
            check_sets[name] = definition
            check_sets_file[name] = file

    # `tables:` flattening is the second pass over file_entries, now that
    # check_sets is fully composed -- a `use:` in the root config can
    # resolve to a set an included file declares, and vice versa.
    raw_checks: list[dict] = []
    check_files: list[Path] = []
    for file, flat, tables, _raw_check_sets in file_entries:
        file_checks, file_files, file_problems = _collect_file_checks(
            flat, tables, file, collect_all_errors, check_sets
        )
        raw_checks.extend(file_checks)
        check_files.extend(file_files)
        problems.extend(file_problems)

    # A `tables:` problem found while assembling raw_checks above (an
    # unknown table field, a nested check restating `source`/`object`, a
    # malformed entry) is raised here -- after the undefined-variable
    # check but before a single Check is built -- rather than inside
    # _collect_file_checks itself, so an undefined ${VAR} is still always
    # reported first regardless of what else is wrong with the config.
    if not collect_all_errors and problems:
        raise ValueError(problems[0].message)

    sources = {
        name: SourceConfig(
            name=name,
            type=spec["type"],
            params={
                k: v for k, v in spec.items() if k not in _SOURCE_OWN_FIELDS
            },
            timezone=spec.get("timezone"),
            timeout=spec.get("timeout"),
        )
        for name, spec in (data.get("sources") or {}).items()
    }

    defaults = data.get("defaults") or {}

    # A check that fails to build is either fatal (the default, matching
    # `[_build_check(raw, defaults) for raw in raw_checks]`'s old
    # first-failure behavior exactly) or, when collecting, skipped and
    # recorded as a ConfigProblem -- appended to the same `problems` list
    # `_collect_file_checks` may have already started above -- while
    # `checks`/`checks_raw`/`checks_files` then hold only the ones that
    # built, kept in lockstep for _validate_checks's zip below.
    checks: list[Check] = []
    checks_raw: list[dict] = []
    checks_files: list[Path] = []
    for raw, file in zip(raw_checks, check_files, strict=True):
        try:
            check = _build_check(raw, defaults)
        except (KeyError, TypeError, ValueError) as exc:
            if not collect_all_errors:
                raise
            problems.append(
                ConfigProblem(
                    files=(file,),
                    message=(
                        f"{_describe_raw_check(raw)}: "
                        f"{_config_error_text(exc)}"
                    ),
                )
            )
            continue
        checks.append(check)
        checks_raw.append(raw)
        checks_files.append(file)

    for check in checks:
        source = sources.get(check.source)
        if source is not None and source.timezone:
            check.source_timezone = source.timezone

    calendar_raw = data.get("calendar")
    calendar = build_calendar(calendar_raw) if calendar_raw else None

    source_errors = _validate_sources(sources)
    check_errors = _validate_checks(checks_raw, checks, sources, calendar)

    if collect_all_errors:
        # Sources are root-only (an included file may declare only
        # `checks:`), so every source problem is attributed to the root
        # config file.
        problems.extend(
            ConfigProblem(files=(path,), message=str(error))
            for error in source_errors
        )
        # Deduplicated, order preserved: a duplicate check_id names two
        # checks, and when both live in the same file -- the ordinary case
        # -- that is one file, not the same file twice. A report groups by
        # file, so a repeated entry here would list the problem twice
        # under the one file it belongs to.
        problems.extend(
            ConfigProblem(
                files=tuple(dict.fromkeys(checks_files[i] for i in indices)),
                message=str(error),
            )
            for indices, error in check_errors
        )
        problems.extend(
            ConfigProblem(
                files=(file,),
                message=f"undefined environment variable: {name}",
            )
            for file, names in missing_by_file.items()
            for name in sorted(names)
        )
    else:
        errors = source_errors + [error for _, error in check_errors]
        if errors:
            _raise_validation_errors(errors)

    return (
        Config(
            sources=sources,
            checks=checks,
            config_dir=config_dir,
            store=_parse_store(data.get("store")),
            calendar=calendar,
        ),
        frozenset(missing),
        problems,
    )
