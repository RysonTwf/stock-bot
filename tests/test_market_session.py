"""Standalone test for market_session()'s NYSE holiday awareness.

No pytest needed:  python tests/test_market_session.py

Bug under test: market_session() used to only check weekday, so on a full
market-closure holiday that falls on a weekday it still labeled the brief
"pre-market"/"regular hours"/"after-hours" instead of "market closed". Repro:
2026-07-04 (Independence Day) falls on a Saturday, so NYSE observes the
holiday on the preceding Friday, 2026-07-03 -- a weekday that would otherwise
sit squarely in the 4:00-9:30 ET pre-market window.
"""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import watchlist

ET = ZoneInfo("America/New_York")


def at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def run_tests():
    # 1. Observed Independence Day (July 4, 2026 is a Saturday -> observed Fri
    #    July 3) must read "market closed" even inside the old pre-market window.
    assert watchlist.market_session(at(2026, 7, 3, 4, 30)) == "market closed"
    assert watchlist.market_session(at(2026, 7, 3, 10, 0)) == "market closed"
    print("PASS observed Independence Day (2026-07-03): market closed")

    # 2. The regular weekday right before it is untouched.
    assert watchlist.market_session(at(2026, 7, 2, 4, 30)) == "pre-market"
    assert watchlist.market_session(at(2026, 7, 2, 10, 0)) == "regular hours"
    assert watchlist.market_session(at(2026, 7, 2, 17, 0)) == "after-hours"
    print("PASS normal trading day (2026-07-02): sessions unaffected")

    # 3. Weekends still closed.
    assert watchlist.market_session(at(2026, 7, 4, 10, 0)) == "market closed"
    print("PASS weekend (2026-07-04): market closed")

    # 4. A spread of other fixed NYSE holidays across the year.
    for y, m, d, label in [
        (2026, 1, 1, "New Year's Day"),
        (2026, 1, 19, "MLK Day (observed)"),
        (2026, 5, 25, "Memorial Day"),
        (2026, 6, 19, "Juneteenth"),
        (2026, 9, 7, "Labor Day"),
        (2026, 11, 26, "Thanksgiving"),
        (2026, 12, 25, "Christmas Day"),
    ]:
        assert watchlist.market_session(at(y, m, d, 10, 0)) == "market closed", label
        print(f"PASS {label} ({y}-{m:02d}-{d:02d}): market closed")

    print("\nAll tests passed.")


if __name__ == "__main__":
    run_tests()
