"""Inbox working hours (inboxes.working_hours, docs/06_DATABASE_ARCHITECTURE.md).

    [{"day": 1, "open": "09:00", "close": "17:00", "closed": false}, ...]

``day`` is ISO weekday: 1 = Monday … 7 = Sunday (0 is accepted as Sunday).
Times are in the inbox's timezone; a day without an entry is closed. ``close``
earlier than ``open`` means the shift runs past midnight.
"""

from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _t(value: str) -> time:
    hours, minutes = value.split(":")[:2]
    return time(int(hours), int(minutes))


def is_open(working_hours: list[dict[str, Any]], timezone: str, now: datetime) -> bool:
    try:
        tz = ZoneInfo(timezone or "UTC")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    local = now.astimezone(tz)
    today, yesterday = local.isoweekday(), (local.isoweekday() - 2) % 7 + 1
    for entry in working_hours:
        if entry.get("closed"):
            continue
        day = int(entry.get("day", -1)) or 7
        try:
            start, end = _t(entry["open"]), _t(entry["close"])
        except (KeyError, ValueError):
            continue
        clock = local.time()
        if start <= end:
            if day == today and start <= clock < end:
                return True
        else:  # overnight: e.g. 22:00 → 06:00
            if (day == today and clock >= start) or (day == yesterday and clock < end):
                return True
    return False
