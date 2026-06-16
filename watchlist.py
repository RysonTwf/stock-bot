"""Shared user watchlist — storage, live quotes (incl. pre/post market), formatting.

The list itself lives in watchlist.json at the repo root. app.py edits it via the
GitHub Contents API so changes persist; bot.py reads the checked-out file in Actions.
"""
import html
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

WATCHLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.json")
MAX_TICKERS = 30
TICKER_RE = re.compile(r"[A-Z0-9.\-]{1,10}")

_ET = ZoneInfo("America/New_York")


def load_watchlist() -> list[str]:
    """Read the shared watchlist from the local checkout."""
    try:
        with open(WATCHLIST_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return [t for t in data if isinstance(t, str)]
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return []


def market_session(now: datetime | None = None) -> str:
    now = now or datetime.now(_ET)
    if now.weekday() >= 5:
        return "market closed"
    minutes = now.hour * 60 + now.minute
    if 4 * 60 <= minutes < 9 * 60 + 30:
        return "pre-market"
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return "regular hours"
    if 16 * 60 <= minutes < 20 * 60:
        return "after-hours"
    return "market closed"


def is_valid_ticker(ticker: str) -> bool:
    """Cheap sanity check that this ticker has live quote data.

    Goes through the same chart-API-first/yfinance-fallback path as quotes
    (_fetch_quote) rather than yf.Ticker().history() directly: the yfinance
    library needs its own crumb/cookie handshake with Yahoo that has proven
    flaky on some hosts (e.g. PythonAnywhere) even while the plain chart API
    _fetch_quote_direct uses keeps working — that mismatch was marking real
    tickers like NVDA as "not found".
    """
    if not TICKER_RE.fullmatch(ticker):
        return False
    return _fetch_quote(ticker) is not None


def _fetch_quote_direct(ticker: str) -> dict | None:
    """Primary quote source: Yahoo's chart API, one call per ticker.

    Returns the live price (pre/post included) plus the previous session close
    from the same response — unlike yfinance daily history, which has returned
    wrongly split-adjusted prev closes (KLAC was off by 10x once).
    """
    resp = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
        params={"range": "1d", "interval": "1m", "includePrePost": "true"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=10,
    )
    resp.raise_for_status()
    result = resp.json()["chart"]["result"][0]
    meta = result["meta"]
    closes = (result.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    bars = [
        (ts, c)
        for ts, c in zip(result.get("timestamp") or [], closes)
        if c is not None
    ]
    if bars:
        last_ts, price = bars[-1]
    else:
        # Cash indices (^GSPC etc.) don't trade pre-market: Yahoo rolls the
        # chart to the new day with no bars at all. The meta still carries the
        # last trade — regularMarketPrice at regularMarketTime (the last
        # close) — which keeps "current price vs prev completed close" intact.
        if not meta.get("regularMarketPrice") or not meta.get("regularMarketTime"):
            return None
        last_ts, price = int(meta["regularMarketTime"]), float(meta["regularMarketPrice"])

    # Yahoo quirk: during pre-market the chart's "current day" is still the
    # prior regular session (the new day's pre-market bars get appended to it),
    # so chartPreviousClose is the close from TWO sessions back — using it
    # compounds the prior day's full move into the pre-market % change
    # (e.g. AMD showed +9.3% instead of ~+2% on 2026-06-12). In that window the
    # last completed close sits in regularMarketPrice instead. Regular hours
    # and after-hours, chartPreviousClose is correct — don't "simplify" this.
    prev = float(meta.get("chartPreviousClose") or 0)
    ctp = meta.get("currentTradingPeriod") or {}
    try:
        in_premarket = ctp["pre"]["start"] <= last_ts < ctp["regular"]["start"]
    except (KeyError, TypeError):
        in_premarket = False
    if in_premarket and meta.get("regularMarketPrice"):
        prev = float(meta["regularMarketPrice"])
    if prev == 0:
        return None
    age = datetime.now(timezone.utc).timestamp() - last_ts
    return {
        "price": float(price),
        "change_pct": (float(price) - prev) / prev * 100,
        "stale": age > 2 * 3600,
    }


# Previous closes don't change intraday, so cache them per ET date. The cache
# lives for the process — useful in the long-running webhook, harmless in Actions.
_prev_close_cache: dict[str, tuple[date, float | None]] = {}  # ticker -> (et_date, prev_close)
_cache_lock = threading.Lock()


def _get_prev_close(ticker: str, et_today: date) -> float | None:
    """Last completed daily close before today (ET)."""
    with _cache_lock:
        hit = _prev_close_cache.get(ticker)
    if hit and hit[0] == et_today:
        return hit[1]

    daily = yf.Ticker(ticker).history(period="10d", interval="1d")["Close"].dropna()
    completed = daily[[ts.date() < et_today for ts in daily.index]]
    prev = float(completed.iloc[-1]) if not completed.empty else None

    with _cache_lock:
        _prev_close_cache[ticker] = (et_today, prev)
    return prev


def _fetch_quote_yfinance(ticker: str) -> dict | None:
    """Fallback quote source via the yfinance library."""
    prev = _get_prev_close(ticker, datetime.now(_ET).date())
    if not prev:
        return None

    hist = yf.Ticker(ticker).history(period="1d", interval="1m", prepost=True)
    closes_1m = hist["Close"].dropna() if not hist.empty else hist
    if closes_1m.empty:
        return None
    age = pd.Timestamp.now(tz=closes_1m.index.tz) - closes_1m.index[-1]
    price = float(closes_1m.iloc[-1])
    return {
        "price": price,
        "change_pct": (price - prev) / prev * 100,
        "stale": age > pd.Timedelta(hours=2),
    }


def _fetch_quote(ticker: str) -> tuple[str, dict] | None:
    quote = None
    try:
        quote = _fetch_quote_direct(ticker)
    except Exception as e:
        print(f"  [warn] Chart-API quote failed for {ticker}: {e}", file=sys.stderr)
    if quote is None:
        try:
            quote = _fetch_quote_yfinance(ticker)
            if quote:
                print(f"  [info] {ticker}: used yfinance fallback", file=sys.stderr)
        except Exception as e:
            print(f"  [warn] yfinance fallback failed for {ticker}: {e}", file=sys.stderr)
    return (ticker, quote) if quote else None


def get_live_quotes(tickers: list[str]) -> dict[str, dict]:
    """Latest traded price (pre/post included) + % change vs last completed close."""
    quotes: dict[str, dict] = {}
    if not tickers:
        return quotes
    with ThreadPoolExecutor(max_workers=10) as ex:
        for fut in as_completed({ex.submit(_fetch_quote, t): t for t in tickers}):
            try:
                item = fut.result()
                if item:
                    quotes[item[0]] = item[1]
            except Exception as e:
                print(f"  [warn] Quote future failed: {e}", file=sys.stderr)
    return quotes


def format_watchlist(tickers: list[str], quotes: dict[str, dict]) -> str:
    """Render the watchlist as Telegram HTML. Numbers come straight from yfinance."""
    if not tickers:
        return ("<b>⭐ Watchlist</b>\n<i>Empty — add stocks with /watch TICKER "
                "(e.g. /watch NVDA TSLA)</i>")
    lines = [f"<b>⭐ Watchlist</b> <i>({market_session()}, vs prev close)</i>"]
    for t in tickers:
        q = quotes.get(t)
        if not q:
            lines.append(f"• {html.escape(t)}: <i>no data</i>")
            continue
        arrow = "▲" if q["change_pct"] >= 0 else "▼"
        tags = " <i>(last trade &gt;2h ago)</i>" if q.get("stale") else ""
        lines.append(f"{arrow} {html.escape(t)}: {q['price']:,.2f} ({q['change_pct']:+.2f}%){tags}")
    return "\n".join(lines)
