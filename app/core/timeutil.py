"""Business calendar helpers (SPEC §0): "today" and working days live in the business TZ.

Working days are Mon–Fri without holidays (decision D-16). All stored timestamps are UTC.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from app.core.config import get_settings


def now_utc() -> datetime:
    return datetime.now(UTC)


def business_today(now: datetime | None = None) -> date:
    return to_business_date(now or now_utc())


def to_business_date(value: datetime) -> date:
    return value.astimezone(get_settings().tz).date()


def business_datetime(day: date, hour: int = 0, minute: int = 0) -> datetime:
    """Aware UTC datetime for a business-local wall-clock time on ``day``."""
    local = datetime.combine(day, time(hour, minute), tzinfo=get_settings().tz)
    return local.astimezone(UTC)


def business_hour(now: datetime | None = None) -> int:
    return (now or now_utc()).astimezone(get_settings().tz).hour


def is_working_day(day: date) -> bool:
    return day.weekday() < 5


def next_working_day(day: date) -> date:
    day += timedelta(days=1)
    while not is_working_day(day):
        day += timedelta(days=1)
    return day


def add_working_days(day: date, n: int) -> date:
    """Move ``n`` working days from ``day`` (``day`` itself need not be a working day)."""
    step = 1 if n >= 0 else -1
    remaining = abs(n)
    while remaining:
        day += timedelta(days=step)
        if is_working_day(day):
            remaining -= 1
    return day


def anchor_d0(today: date) -> date:
    """Seed anchor D0: ``today`` if it is a working day, else the next Monday."""
    return today if is_working_day(today) else next_working_day(today)


def wd(d0: date, n: int) -> date:
    """The n-th working day after ``d0`` (``wd(d0, 0) == d0``, negative = before)."""
    return add_working_days(d0, n)


def week_monday(day: date) -> date:
    return day - timedelta(days=day.weekday())
