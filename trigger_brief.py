"""Kick off the daily brief workflow at the US market open (9:30 AM New York).

GitHub's own cron schedule routinely starts hours late, but workflow_dispatch
runs start within seconds. Run this from a PythonAnywhere scheduled task at
13:30 UTC:

    GITHUB_PAT=... python3.10 /home/<you>/stocks/trigger_brief.py

13:30 UTC is the open during US daylight saving time. In winter the open is
14:30 UTC, so the script waits until then. Weekends and NYSE holidays are
skipped.
"""
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from watchlist import _is_market_holiday

GITHUB_REPO   = "RysonTwf/stock-bot"
WORKFLOW_FILE = "daily_brief.yml"
ET            = ZoneInfo("America/New_York")


def main() -> None:
    now = datetime.now(ET)
    if now.weekday() >= 5 or _is_market_holiday(now.date()):
        print(f"{now:%a %Y-%m-%d} — US market closed, skipping")
        return

    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    wait = (market_open - now).total_seconds()
    if wait > 0:
        print(f"Waiting {wait / 60:.0f} min for the 9:30 ET open")
        time.sleep(wait)

    resp = requests.post(
        f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches",
        headers={
            "Authorization": f"Bearer {os.environ['GITHUB_PAT']}",
            "Accept":        "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"ref": "main"},
        timeout=15,
    )
    if resp.status_code != 204:
        sys.exit(f"Dispatch failed: {resp.status_code} {resp.text[:300]}")
    print("Daily brief dispatched")


if __name__ == "__main__":
    main()
