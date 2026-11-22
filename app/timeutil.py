"""UTC-day helpers.

All persisted interval endpoints are integer UTC epoch seconds.  A daily
record covers exactly ``[00:00:00Z, next 00:00:00Z)``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta


def day_bounds(day: date) -> tuple[int, int]:
    """Return the inclusive start / exclusive end of a UTC day as epoch seconds."""

    start = datetime.combine(day, time.min, tzinfo=UTC)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def format_epoch(value: int) -> str:
    """Format epoch seconds as an ISO-8601 UTC timestamp with second precision."""

    return datetime.fromtimestamp(value, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    """Current UTC time as a second-precision ISO-8601 timestamp."""

    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
