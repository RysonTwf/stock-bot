"""Kick off the daily brief workflow on time.

GitHub's own cron schedule routinely starts hours late, but workflow_dispatch
runs start within seconds. Run this from a PythonAnywhere scheduled task at
08:00 UTC (16:00 SGT):

    GITHUB_PAT=... python3 /home/<you>/stock-bot/trigger_brief.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

GITHUB_REPO   = "RysonTwf/stock-bot"
WORKFLOW_FILE = "daily_brief.yml"
SGT           = timezone(timedelta(hours=8))


def main() -> None:
    today = datetime.now(SGT)
    if today.weekday() >= 5:  # PythonAnywhere tasks run daily; brief is Mon–Fri SGT
        print(f"{today:%a %Y-%m-%d} is a weekend in SGT — skipping")
        return

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
