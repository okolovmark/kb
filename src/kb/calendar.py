"""Working-day calendar: Mon-Fri minus country holidays minus extra days off; today in the kb tz."""

import datetime as dt
import functools
import os
from zoneinfo import ZoneInfo

import holidays

from kb.config import Config

SATURDAY = 5


@functools.lru_cache(maxsize=8)
def _holidays(
    country: str, extra: tuple[str, ...]
) -> tuple[holidays.HolidayBase, frozenset[dt.date]]:
    return holidays.country_holidays(country), frozenset(dt.date.fromisoformat(d) for d in extra)


def is_working_day(day: dt.date, cfg: Config) -> bool:
    """Monday-Friday, not a ``[calendar].country`` holiday, not in ``extra_days_off``."""
    if day.weekday() >= SATURDAY:
        return False
    country, extra = _holidays(cfg.calendar.country, cfg.calendar.extra_days_off)
    return day not in extra and day not in country


def holiday_name(day: dt.date, cfg: Config) -> str | None:
    """The holiday name for *day*, ``extra day off`` for a configured one, None on a normal day."""
    country, extra = _holidays(cfg.calendar.country, cfg.calendar.extra_days_off)
    if day in extra:
        return "extra day off"
    return country.get(day)


def working_days_between(start: dt.date, end: dt.date, cfg: Config) -> int:
    """Working days in ``[start, end)``: the anchor day counts once it has begun, today does not."""
    if end <= start:
        return 0
    return sum(1 for n in range((end - start).days) if is_working_day(start + dt.timedelta(n), cfg))


def today(cfg: Config) -> dt.date:
    """The current date in ``[general].timezone``; ``KB_TODAY=YYYY-MM-DD`` overrides it."""
    override = os.environ.get("KB_TODAY")
    if override:
        return dt.date.fromisoformat(override)
    return dt.datetime.now(ZoneInfo(cfg.general.timezone)).date()


def now(cfg: Config | None = None) -> dt.datetime:
    """Current UTC instant, the timestamp every event carries.

    Under ``KB_TODAY`` the date part follows the override (the time of day stays real), so
    events written in a replayed day land on that day in the kb timezone.
    """
    real = dt.datetime.now(dt.UTC)
    override = os.environ.get("KB_TODAY")
    if not override or cfg is None:
        return real
    tz = ZoneInfo(cfg.general.timezone)
    local = dt.datetime.combine(dt.date.fromisoformat(override), real.astimezone(tz).timetz())
    return local.astimezone(dt.UTC)


def local_date(at: dt.datetime, cfg: Config) -> dt.date:
    """The kb-timezone date of a UTC instant (events are UTC, schedules are local dates)."""
    return at.astimezone(ZoneInfo(cfg.general.timezone)).date()


def day_start_utc(day: dt.date, cfg: Config) -> dt.datetime:
    """Midnight of *day* in the kb timezone, as a UTC instant (migrated event timestamps)."""
    local = dt.datetime.combine(day, dt.time(), tzinfo=ZoneInfo(cfg.general.timezone))
    return local.astimezone(dt.UTC)
