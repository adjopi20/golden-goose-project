"""Explicit New York session clock; UTC epoch milliseconds, including DST."""
from datetime import date, datetime, time
from zoneinfo import ZoneInfo


NY = ZoneInfo("America/New_York")


def clock_ms(day: date, hour: int, minute: int = 0) -> int:
    return int(datetime.combine(day, time(hour, minute), NY).timestamp() * 1000)
