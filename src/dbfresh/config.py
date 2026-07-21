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

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


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


_INCLUDED_FILE_ALLOWED_KEY = "checks"


def _load_included_checks(raw: Any, path: Path) -> list[dict]:
    """Normalize an included file's parsed YAML into a list of check blocks.

    An included file contributes only checks: a bare sequence of check
    blocks, or a mapping with a single ``checks:`` key. ``include:``,
    ``sources:``, ``calendar:``, ``store:``, and ``defaults:`` may appear
    only in the root config, so any other top-level key here is a
    validation error.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        extra = sorted(set(raw) - {_INCLUDED_FILE_ALLOWED_KEY})
        if extra:
            raise ValueError(
                f"included file {path} may only declare a top-level "
                f"'checks:' key; found disallowed key(s): {extra}"
            )
        return raw.get("checks") or []
    raise ValueError(
        f"included file {path} must be a checks list or a {{checks: [...]}} mapping"
    )


def _read_included_file(
    path: Path, env: dict[str, str] | None, missing: set[str] | None = None
) -> list[dict]:
    raw = yaml.safe_load(path.read_text())
    raw = interpolate_env(raw, env, missing)
    return _load_included_checks(raw, path)


def _load_config_or_raise(
    path: str | Path, env: dict[str, str] | None, collect_missing: bool
) -> tuple[Config, frozenset[str]]:
    """Shared exception-translation boundary for :func:`load_config` and
    :func:`load_config_tolerant` -- both call :func:`_load_config` and
    turn every failure mode into a single :class:`ConfigError`, chained
    from its underlying cause: a missing or unreadable file, a YAML parse
    error, a missing required field, an invalid expectation, or any of
    the validation checks in :func:`_validate_checks` / :func:`_validate_sources`.
    """
    try:
        return _load_config(path, env, collect_missing=collect_missing)
    except ConfigError:
        raise
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    except KeyError as exc:
        raise ConfigError(f"missing required field: {exc}") from exc
    except TypeError as exc:
        raise ConfigError(f"invalid expectation: {exc}") from exc
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


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
    config, _missing = _load_config_or_raise(path, env, collect_missing=False)
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
    return _load_config_or_raise(path, env, collect_missing=True)


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
) -> list[ValueError]:
    """Collect every check-level validation problem instead of raising on
    the first one found.

    Covers: unknown source references, unknown metrics, missing
    discriminating fields, a metric check with no expectation, a check with
    none of metric/assert/assert_sql (or more than one of them -- exactly
    one primitive is required), unknown check-block keys, an invalid
    ``severity``, ``max_lag`` used outside ``freshness``, freshness-source
    problems (missing column, dialect capability), duplicate ``check_id``s,
    and calendar features used without a top-level ``calendar:`` block.
    """
    errors: list[ValueError] = []
    seen: dict[str, Check] = {}
    metric_names = {spec.name for spec in METRICS}

    for raw, check in zip(raw_checks, checks, strict=True):
        label = describe_check(check)

        extra_keys = sorted(set(raw) - _CHECK_KEYS)
        if extra_keys:
            errors.append(
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
            errors.append(
                ValueError(
                    f"{label}: check has none of metric, assert, or assert_sql"
                )
            )
        elif len(primitives) > 1:
            errors.append(
                ValueError(
                    f"{label}: check has more than one of metric/assert/assert_sql "
                    f"({', '.join(primitives)}) -- a check must set exactly one"
                )
            )

        if check.severity not in _VALID_SEVERITIES:
            errors.append(
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
            errors.append(
                ValueError(
                    f"{label}: 'max_lag' is only valid for the freshness metric"
                )
            )

        if check.source not in sources:
            errors.append(
                ValueError(
                    f"check references unknown source: {check.source!r}"
                )
            )
        elif check.metric is not None and check.metric not in metric_names:
            errors.append(
                ValueError(f"{label}: unknown metric: {check.metric!r}")
            )
        elif check.metric is not None:
            errors.extend(_validate_metric_fields(check, label))
            errors.extend(_validate_freshness_source(check, sources, label))

        if not calendar and (
            check.by_weekday
            or check.on_holiday is not None
            or check.calendar == "business"
            or check.skip_off_schedule
        ):
            errors.append(
                ValueError(
                    f"check on {check.object!r} uses calendar features "
                    "(by_weekday/on_holiday/calendar/skip_off_schedule) but no "
                    "top-level calendar: block is configured"
                )
            )

        cid = check_id(check)
        if cid in seen:
            errors.append(
                ValueError(
                    f"duplicate check_id {cid!r}: {describe_check(seen[cid])} and "
                    f"{label} collide -- add an explicit id: to "
                    "one of them to disambiguate"
                )
            )
        else:
            seen[cid] = check

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
) -> tuple[Config, frozenset[str]]:
    """``collect_missing`` mirrors :func:`interpolate_env`'s own ``missing``
    parameter: false (the default, used by :func:`load_config`) raises on
    any undefined ``${VAR}``; true (used by :func:`load_config_tolerant`)
    collects every undefined name into the returned set instead, leaving
    the ``${VAR}`` token literal in place rather than resolving it.
    """
    path = Path(path)
    config_dir = path.resolve().parent
    data = yaml.safe_load(path.read_text()) or {}
    missing: set[str] = set()
    data = interpolate_env(data, env, missing)

    raw_checks = list(data.get("checks") or [])
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
            raw_checks.extend(_read_included_file(include_path, env, missing))

    if missing and not collect_missing:
        names = ", ".join(sorted(missing))
        raise ConfigError(
            f"undefined environment variable: {names}"
            if len(missing) == 1
            else f"undefined environment variables: {names}"
        )

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

    checks = [_build_check(raw, defaults) for raw in raw_checks]
    for check in checks:
        source = sources.get(check.source)
        if source is not None and source.timezone:
            check.source_timezone = source.timezone

    calendar_raw = data.get("calendar")
    calendar = build_calendar(calendar_raw) if calendar_raw else None

    errors = _validate_sources(sources) + _validate_checks(
        raw_checks, checks, sources, calendar
    )
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
    )
