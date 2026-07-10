"""Load, interpolate, and validate check configuration."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from dbfresh.checks import Check, parse_expectation

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


@dataclass
class Config:
    sources: dict[str, SourceConfig]
    checks: list[Check]


def load_config(path: str | Path, env: dict[str, str] | None = None) -> Config:
    """Parse a YAML config, interpolate secrets, and validate references."""
    path = Path(path)
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

    checks = []
    for raw in data.get("checks") or []:
        expect = parse_expectation(raw["expect"]) if "expect" in raw else None
        checks.append(
            Check(
                source=raw["source"],
                object=raw["object"],
                metric=raw.get("metric"),
                column=raw.get("column"),
                key=raw.get("key"),
                where=raw.get("where"),
                assert_=raw.get("assert"),
                expect=expect,
                allow_empty=raw.get("allow_empty", False),
                severity=raw.get("severity", "error"),
                id=raw.get("id"),
            )
        )

    for check in checks:
        if check.source not in sources:
            raise ValueError(f"check references unknown source: {check.source!r}")

    return Config(sources=sources, checks=checks)
