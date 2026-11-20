"""Standalone test for wait_for_open.seconds_until_open().

No pytest needed:  python tests/test_wait_for_open.py

The scheduled run starts early (GitHub's cron runs hours late, unpredictably)
and waits for the 9:30 AM New York open, following US daylight saving time.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wait_for_open


def utc(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def run_tests():
    # Winter (EST): open is 14:30 UTC. Cron fires on time at 08:45 UTC.
    assert wait_for_open.seconds_until_open(utc(2026, 11, 12, 8, 45)) == 5 * 3600 + 45 * 60

    # Summer (EDT): open is 13:30 UTC.
    assert wait_for_open.seconds_until_open(utc(2026, 6, 10, 8, 45)) == 4 * 3600 + 45 * 60

    # GitHub started it after the open: no wait, send now.
    assert wait_for_open.seconds_until_open(utc(2026, 11, 12, 21, 29)) == 0

    # Exactly at the open.
    assert wait_for_open.seconds_until_open(utc(2026, 11, 12, 14, 30)) == 0

    # Cron at 04:45 UTC, GitHub ~9h late (the norm in Nov 2026): waits for the open.
    assert wait_for_open.seconds_until_open(utc(2026, 11, 20, 13, 40)) == 50 * 60

    # GitHub on time: a ~9h45m wait would hit the 6-hour job limit and send
    # nothing, so the wait is capped and the brief goes out early instead.
    # 04:45 UTC is 23:45 the previous day in New York: still waits for *this*
    # morning's open rather than treating yesterday's as passed and sending now.
    assert wait_for_open.seconds_until_open(utc(2026, 11, 20, 4, 45)) == wait_for_open.MAX_WAIT
    assert wait_for_open.MAX_WAIT < 355 * 60

    print("All tests passed.")


if __name__ == "__main__":
    run_tests()
