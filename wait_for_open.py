"""Hold a scheduled brief until the 9:30 AM New York open.

GitHub starts scheduled workflows anywhere from minutes to ~9 hours late, so
the cron fires early (08:45 UTC) and this waits out the rest. If GitHub ran it
after the open, it returns at once and the brief goes out late but immediately.
"""
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def seconds_until_open(now: datetime) -> int:
    now_et = now.astimezone(ET)
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    return max(0, int((market_open - now_et).total_seconds()))


if __name__ == "__main__":
    wait = seconds_until_open(datetime.now(timezone.utc))
    print(f"Waiting {wait // 60} min for the 9:30 AM New York open")
    time.sleep(wait)
