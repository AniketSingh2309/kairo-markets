"""Injectable clock so freshness logic is testable against a fixed reference time."""

from __future__ import annotations

import datetime as dt
from typing import Callable

Clock = Callable[[], dt.datetime]


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def fixed_clock(at: dt.datetime) -> Clock:
    if at.tzinfo is None:
        raise ValueError("fixed_clock requires a timezone-aware datetime")
    return lambda: at
