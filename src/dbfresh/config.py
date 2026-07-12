"""Load, interpolate, and validate check configuration."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from dbfresh.calendar import WEEKDAY_NAMES, BusinessCalendar, build_calendar
from dbfresh.checks import Check, check_id, parse_expectation

_CHECK_CALENDAR_MODES = frozenset({"business"})

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def interpolate_env(value: Any, env: dict[str, str] | None = None) -> Any:
    """Replace ``${VAR}`` tokens in strings from ``env`` (default the process env).

    A referenced variable that is not set is a hard error.
    """
    environ = os.environ if env is None else env

    if isinstance(value, str):

        def replace(match: re.Match) -> str:
            name = match.group(1)
            if name not in environ:
                raise ValueError(f"undefined environment variable: {name}")
            return environ[name]

        return _VAR.sub(replace, value)
    if isinstance(value, dict):
        return {key: interpolate_env(item, environ) for key, item in value.items()}
    if isinstance(value, list):
        return [interpolate_env(item, environ) for item in value]
    return value


@dataclass
class SourceConfig:
    name: str
    type: str
    params: dict


_DEFAULT_RETAIN_DAYS = 400


@dataclass
class StoreConfig:
    """Observation-store settings (spec section 8.1)."""

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


def _parse_by_weekday(raw: Any, metric: str | None = None) -> dict[str, Any] | None:
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


def _build_check(raw: dict, defaults: dict) -> Check:
    """Build one Check, merging ``defaults:`` fields the check itself omits.

    Merged fields are ``severity``, ``calendar``, ``where``, ``allow_empty``,
    and ``skip_off_schedule`` (§12.1); a per-check value always overrides the
    default, including an explicit falsy value such as ``allow_empty: false``.
    """
    metric = raw.get("metric")
    expect = (
        parse_expectation(raw["expect"], metric=metric) if "expect" in raw else None
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
        expect=expect,
        allow_empty=raw.get("allow_empty", defaults.get("allow_empty", False)),
        severity=raw.get("severity", defaults.get("severity", "error")),
        id=raw.get("id"),
        by_weekday=_parse_by_weekday(raw.get("by_weekday"), metric=metric),
        on_holiday=(
            parse_expectation(on_holiday, metric=metric) if on_holiday else None
        ),
        calendar=_parse_check_calendar_mode(
            raw.get("calendar", defaults.get("calendar"))
        ),
        skip_off_schedule=raw.get(
            "skip_off_schedule", defaults.get("skip_off_schedule", False)
        ),
    )


def _resolve_includes(config_dir: Path, patterns: Any) -> list[Path]:
    """Resolve root-only ``include:`` globs (§12.2) to matched files.

    Each glob is relative to ``config_dir`` — the root config's directory,
    never the process CWD (§12.3). A glob matching no files is a validation
    error (a mistyped include must not silently drop checks). Matched files
    across all globs are deduplicated and returned in lexicographic path
    order; the load order itself carries no semantics.
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
    validation error (§12.2).
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


def _read_included_file(path: Path, env: dict[str, str] | None) -> list[dict]:
    raw = yaml.safe_load(path.read_text())
    raw = interpolate_env(raw, env)
    return _load_included_checks(raw, path)


def load_config(path: str | Path, env: dict[str, str] | None = None) -> Config:
    """Parse a YAML config, interpolate secrets, and validate references.

    Supports composition (§12.2): the root config's ``include:`` list of
    path globs, resolved relative to the root config's directory, whose
    matched files each contribute a ``checks:`` list merged with the root
    file's own. The composed check list is validated as one unit, so a
    duplicate ``check_id`` anywhere across the root and included files is a
    validation error.
    """
    path = Path(path)
    config_dir = path.resolve().parent
    data = yaml.safe_load(path.read_text()) or {}
    data = interpolate_env(data, env)

    sources = {
        name: SourceConfig(
            name=name,
            type=spec["type"],
            params={k: v for k, v in spec.items() if k != "type"},
        )
        for name, spec in (data.get("sources") or {}).items()
    }

    defaults = data.get("defaults") or {}

    raw_checks = list(data.get("checks") or [])
    include_patterns = data.get("include")
    if include_patterns:
        for include_path in _resolve_includes(config_dir, include_patterns):
            raw_checks.extend(_read_included_file(include_path, env))

    checks = [_build_check(raw, defaults) for raw in raw_checks]

    for check in checks:
        if check.source not in sources:
            raise ValueError(f"check references unknown source: {check.source!r}")

    seen_ids: set[str] = set()
    for check in checks:
        cid = check_id(check)
        if cid in seen_ids:
            raise ValueError(f"duplicate check_id across composed config: {cid!r}")
        seen_ids.add(cid)

    calendar_raw = data.get("calendar")
    calendar = build_calendar(calendar_raw) if calendar_raw else None

    if calendar is None:
        for check in checks:
            if (
                check.by_weekday
                or check.on_holiday is not None
                or check.calendar == "business"
                or check.skip_off_schedule
            ):
                raise ValueError(
                    f"check on {check.object!r} uses calendar features "
                    "(by_weekday/on_holiday/calendar/skip_off_schedule) but no "
                    "top-level calendar: block is configured"
                )

    return Config(
        sources=sources,
        checks=checks,
        config_dir=config_dir,
        store=_parse_store(data.get("store")),
        calendar=calendar,
    )
