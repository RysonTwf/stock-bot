"""Standalone test for _fetch_quote_direct()'s prev-close selection.

No pytest needed:  python tests/test_prev_close.py

Yahoo quirk under test: during pre-market the chart's "current day" is still
the prior regular session, so chartPreviousClose is two sessions back and the
last completed close lives in regularMarketPrice. Regular hours / after-hours,
chartPreviousClose is the right prev close.

Run manually to also see a live AMD/NVDA fetch to eyeball vs Google Finance.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import watchlist

# Synthetic trading day (epoch seconds): pre 4:00-9:30, regular 9:30-16:00, post 16:00-20:00.
PRE_START = 1_750_000_000
REG_START = PRE_START + int(5.5 * 3600)
REG_END = REG_START + int(6.5 * 3600)
POST_END = REG_END + 4 * 3600

# Numbers from the live AMD repro on 2026-06-12: chartPreviousClose was the
# June 10 close, the actual June 11 close sat in regularMarketPrice.
STALE_CLOSE = 452.40   # chartPreviousClose (two sessions back during pre-market)
REAL_CLOSE = 488.45    # regularMarketPrice (last completed close)
PRICE = 498.22


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def payload(last_ts, price, regular_market_price=REAL_CLOSE):
    return {"chart": {"result": [{
        "meta": {
            "chartPreviousClose": STALE_CLOSE,
            "regularMarketPrice": regular_market_price,
            "currentTradingPeriod": {
                "pre": {"start": PRE_START, "end": REG_START},
                "regular": {"start": REG_START, "end": REG_END},
                "post": {"start": REG_END, "end": POST_END},
            },
        },
        "timestamp": [last_ts - 60, last_ts],
        "indicators": {"quote": [{"close": [None, price]}]},
    }]}}


def fetch_with(mock_payload):
    original = watchlist.requests.get
    watchlist.requests.get = lambda *a, **k: FakeResponse(mock_payload)
    try:
        return watchlist._fetch_quote_direct("TEST")
    finally:
        watchlist.requests.get = original


def expected_pct(price, prev):
    return (price - prev) / prev * 100


def run_tests():
    # 1. Pre-market of a new day: last bar inside [pre.start, regular.start)
    #    -> prev must be regularMarketPrice, not the stale chartPreviousClose.
    q = fetch_with(payload(PRE_START + 3600, PRICE))
    assert q is not None
    assert abs(q["change_pct"] - expected_pct(PRICE, REAL_CLOSE)) < 1e-9, q
    print("PASS pre-market: prev = regularMarketPrice "
          f"({REAL_CLOSE}, change {q['change_pct']:+.2f}%)")

    # 2. Regular hours: last bar at/after regular.start -> chartPreviousClose.
    q = fetch_with(payload(REG_START + 3600, PRICE))
    assert q is not None
    assert abs(q["change_pct"] - expected_pct(PRICE, STALE_CLOSE)) < 1e-9, q
    print("PASS regular hours: prev = chartPreviousClose "
          f"({STALE_CLOSE}, change {q['change_pct']:+.2f}%)")

    # 3. After-hours: last bar after regular.end -> chartPreviousClose.
    q = fetch_with(payload(REG_END + 3600, PRICE))
    assert q is not None
    assert abs(q["change_pct"] - expected_pct(PRICE, STALE_CLOSE)) < 1e-9, q
    print("PASS after-hours: prev = chartPreviousClose "
          f"({STALE_CLOSE}, change {q['change_pct']:+.2f}%)")

    # 4. Guard: pre-market but regularMarketPrice missing/0 -> fall back to
    #    chartPreviousClose rather than crashing or returning None.
    q = fetch_with(payload(PRE_START + 3600, PRICE, regular_market_price=0))
    assert q is not None
    assert abs(q["change_pct"] - expected_pct(PRICE, STALE_CLOSE)) < 1e-9, q
    print("PASS pre-market with missing regularMarketPrice: fell back to chartPreviousClose")

    # 5. No bars at all (cash index pre-market: Yahoo rolls the chart to the
    #    new day, empty) -> quote from meta: regularMarketPrice (the last
    #    close, timestamped in the prior regular session) vs chartPreviousClose.
    p = {"chart": {"result": [{
        "meta": {
            "chartPreviousClose": STALE_CLOSE,
            "regularMarketPrice": REAL_CLOSE,
            "regularMarketTime": REG_START - 18 * 3600,  # prior session's close
            "currentTradingPeriod": {
                "pre": {"start": PRE_START, "end": REG_START},
                "regular": {"start": REG_START, "end": REG_END},
                "post": {"start": REG_END, "end": POST_END},
            },
        },
        "indicators": {"quote": [{}]},
    }]}}
    q = fetch_with(p)
    assert q is not None
    assert abs(q["price"] - REAL_CLOSE) < 1e-9, q
    assert abs(q["change_pct"] - expected_pct(REAL_CLOSE, STALE_CLOSE)) < 1e-9, q
    print("PASS index with no bars: price/prev taken from meta "
          f"({REAL_CLOSE} vs {STALE_CLOSE}, change {q['change_pct']:+.2f}%)")

    print("\nAll tests passed.")


def live_check():
    print("\nLive check (compare against Google Finance):")
    for ticker in ("AMD", "NVDA"):
        q = watchlist._fetch_quote_direct(ticker)
        if not q:
            print(f"  {ticker}: no data")
            continue
        # Back out the prev close the function chose from price and % change.
        prev = q["price"] / (1 + q["change_pct"] / 100)
        stale = "  (stale: last trade >2h ago)" if q.get("stale") else ""
        print(f"  {ticker}: price {q['price']:,.2f}  prev close {prev:,.2f}  "
              f"change {q['change_pct']:+.2f}%{stale}")


if __name__ == "__main__":
    run_tests()
    live_check()
