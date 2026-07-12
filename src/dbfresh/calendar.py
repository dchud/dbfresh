"""Business calendar: weekend and holiday awareness (spec section 7)."""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

import holidays as holidays_pkg

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
        country_holidays: Any | None = None,
        extra: frozenset[dt.date] = frozenset(),
        remove: frozenset[dt.date] = frozenset(),
    ) -> None:
        self.timezone = timezone
        self.workdays = workdays
        self._zone = ZoneInfo(timezone)
        self._country_holidays = country_holidays
        self._extra = extra
        self._remove = remove

    @property
    def zone(self) -> ZoneInfo:
        """The calendar's timezone, as a :class:`~zoneinfo.ZoneInfo`."""
        return self._zone

    def local_date(self, when: dt.datetime) -> dt.date:
        """``when`` converted to the calendar timezone, then reduced to a date."""
        return when.astimezone(self._zone).date()

    def is_holiday(self, day: dt.date) -> bool:
        """A holiday: country holidays unioned with ``extra``, minus ``remove``."""
        if day in self._remove:
            return False
        if day in self._extra:
            return True
        return self._country_holidays is not None and day in self._country_holidays

    def is_business_day(self, day: dt.date) -> bool:
        return day.weekday() in self.workdays and not self.is_holiday(day)

    def previous_business_day(self, day: dt.date) -> dt.date:
        candidate = day - dt.timedelta(days=1)
        while not self.is_business_day(candidate):
            candidate -= dt.timedelta(days=1)
        return candidate

    def business_time_between(self, t0: dt.datetime, t1: dt.datetime) -> dt.timedelta:
        """Wall-clock elapsed minus 24h per non-business date strictly between
        ``t0`` and ``t1``'s calendar dates (spec section 7.3).

        Both timestamps are converted to the calendar timezone before their
        dates are compared; ``t0`` must be at or before ``t1``.
        """
        elapsed = t1 - t0
        d0 = self.local_date(t0)
        d1 = self.local_date(t1)
        non_business_days = 0
        day = d0 + dt.timedelta(days=1)
        while day < d1:
            if not self.is_business_day(day):
                non_business_days += 1
            day += dt.timedelta(days=1)
        return elapsed - dt.timedelta(hours=24) * non_business_days


def _parse_workdays(raw: Any) -> frozenset[int]:
    names = raw if raw is not None else _DEFAULT_WORKDAYS
    try:
        return frozenset(_WEEKDAY_INDEX[name] for name in names)
    except KeyError as exc:
        raise ValueError(f"unknown workday name: {exc.args[0]!r}") from exc


def _parse_dates(raw: Any) -> frozenset[dt.date]:
    return frozenset(dt.date.fromisoformat(text) for text in raw or [])


def build_calendar(raw: dict) -> BusinessCalendar:
    """Build a :class:`BusinessCalendar` from the top-level ``calendar:`` block."""
    timezone = raw.get("timezone")
    if not timezone:
        raise ValueError("calendar.timezone is required")
    workdays = _parse_workdays(raw.get("workdays"))
    holiday_raw = raw.get("holidays") or {}
    country = holiday_raw.get("country")
    subdivision = holiday_raw.get("subdivision")
    country_holidays = (
        holidays_pkg.country_holidays(country, subdiv=subdivision) if country else None
    )
    extra = _parse_dates(holiday_raw.get("extra"))
    remove = _parse_dates(holiday_raw.get("remove"))
    return BusinessCalendar(
        timezone=timezone,
        workdays=workdays,
        country_holidays=country_holidays,
        extra=extra,
        remove=remove,
    )
