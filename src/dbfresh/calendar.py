"""Business calendar: weekend and holiday awareness (spec section 7)."""

from __future__ import annotations

import datetime as dt
from typing import Any

WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WEEKDAY_INDEX = {name: index for index, name in enumerate(WEEKDAY_NAMES)}
_DEFAULT_WORKDAYS = ("mon", "tue", "wed", "thu", "fri")


def weekday_key(day: dt.date) -> str:
    """The 3-letter weekday key (``mon``..``sun``) for a calendar date."""
    return WEEKDAY_NAMES[day.weekday()]


class BusinessCalendar:
    """A weekday + holiday aware calendar (spec section 7.1)."""

    def __init__(
        self,
        timezone: str,
        workdays: frozenset[int],
    ) -> None:
        self.timezone = timezone
        self.workdays = workdays

    def is_business_day(self, day: dt.date) -> bool:
        return day.weekday() in self.workdays


def _parse_workdays(raw: Any) -> frozenset[int]:
    names = raw if raw is not None else _DEFAULT_WORKDAYS
    try:
        return frozenset(_WEEKDAY_INDEX[name] for name in names)
    except KeyError as exc:
        raise ValueError(f"unknown workday name: {exc.args[0]!r}") from exc


def build_calendar(raw: dict) -> BusinessCalendar:
    """Build a :class:`BusinessCalendar` from the top-level ``calendar:`` block."""
    timezone = raw.get("timezone")
    if not timezone:
        raise ValueError("calendar.timezone is required")
    workdays = _parse_workdays(raw.get("workdays"))
    return BusinessCalendar(timezone=timezone, workdays=workdays)
