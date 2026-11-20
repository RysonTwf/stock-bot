"""Hold a scheduled brief until the 9:30 AM New York open.

GitHub starts scheduled workflows anywhere from minutes to ~10 hours late, so
the cron fires early (04:45 UTC) and this waits out the rest. If GitHub ran it
after the open, it returns at once and the brief goes out late but immediately.
The wait is capped so a punctual start can't hit GitHub's 6-hour job limit
and send nothing; the brief goes out early instead.
"""
import time
from datetime import datetime, time as clock, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MAX_WAIT = 5 * 3600 + 45 * 60  # job limit is 6h; leave room to send the brief


def seconds_until_open(now: datetime) -> int:
    # The open on the run's UTC date: an 04:45 UTC start is still the previous
    # evening in New York, where "today's 9:30" has already passed
    utc_date = now.astimezone(timezone.utc).date()
    market_open = datetime.combine(utc_date, clock(9, 30), tzinfo=ET)
    return min(MAX_WAIT, max(0, int((market_open - now).total_seconds())))


if __name__ == "__main__":
    wait = seconds_until_open(datetime.now(timezone.utc))
    print(f"Waiting {wait // 60} min for the 9:30 AM New York open")
    time.sleep(wait)
