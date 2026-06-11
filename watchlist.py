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
    """Cheap sanity check that yfinance knows this symbol."""
    if not TICKER_RE.fullmatch(ticker):
        return False
    try:
        return not yf.Ticker(ticker).history(period="5d", interval="1d").empty
    except Exception:
        return False


# Daily closes don't change intraday, so cache them per ET date. The cache
# lives for the process — useful in the long-running webhook, harmless in Actions.
_daily_cache: dict[str, tuple[date, float, float | None]] = {}  # ticker -> (et_date, prev_close, today_close)
_cache_lock = threading.Lock()


def _get_daily_closes(ticker: str, et_today: date, need_today_close: bool) -> tuple[float | None, float | None]:
    """Return (prev_completed_close, today_close). today_close is only fetched
    after the regular session ends (need_today_close), else None."""
    with _cache_lock:
        hit = _daily_cache.get(ticker)
    if hit and hit[0] == et_today and not (need_today_close and hit[2] is None):
        return hit[1], hit[2]

    daily = yf.Ticker(ticker).history(period="10d", interval="1d")["Close"].dropna()
    completed = daily[[ts.date() < et_today for ts in daily.index]]
    prev = float(completed.iloc[-1]) if not completed.empty else None
    today_rows = daily[[ts.date() == et_today for ts in daily.index]]
    today_close = float(today_rows.iloc[-1]) if (need_today_close and not today_rows.empty) else None

    with _cache_lock:
        _daily_cache[ticker] = (et_today, prev, today_close)
    return prev, today_close


def _fetch_quote_yahoo(ticker: str) -> dict | None:
    now_et = datetime.now(_ET)
    et_today = now_et.date()
    # After 16:00 ET on a weekday today's daily bar is settled, so we can split
    # the move into day change and after-hours change
    post_close = now_et.weekday() < 5 and now_et.hour >= 16

    prev, today_close = _get_daily_closes(ticker, et_today, post_close)
    if not prev:
        return None

    hist = yf.Ticker(ticker).history(period="1d", interval="1m", prepost=True)
    closes_1m = hist["Close"].dropna() if not hist.empty else hist
    if closes_1m.empty:
        return None
    age = pd.Timestamp.now(tz=closes_1m.index.tz) - closes_1m.index[-1]
    price = float(closes_1m.iloc[-1])
    quote = {
        "price": price,
        "change_pct": (price - prev) / prev * 100,
        "stale": age > pd.Timedelta(hours=2),
    }
    if today_close:
        quote["day_pct"] = (today_close - prev) / prev * 100
        quote["post_pct"] = (price - today_close) / today_close * 100
    return quote


def _fetch_quote_direct(ticker: str) -> dict | None:
    """Fallback when the yfinance library fails: hit Yahoo's chart API directly.

    Most yfinance breakage is library-side (crumb/cookie/parsing), so a plain
    HTTP call to the same backend usually still works. No day/AH split here.
    """
    resp = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
        params={"range": "1d", "interval": "1m", "includePrePost": "true"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=10,
    )
    resp.raise_for_status()
    result = resp.json()["chart"]["result"][0]
    prev = float(result["meta"]["chartPreviousClose"])
    bars = [
        (ts, c)
        for ts, c in zip(result["timestamp"], result["indicators"]["quote"][0]["close"])
        if c is not None
    ]
    if not bars or prev == 0:
        return None
    last_ts, price = bars[-1]
    age = datetime.now(timezone.utc).timestamp() - last_ts
    return {
        "price": float(price),
        "change_pct": (float(price) - prev) / prev * 100,
        "stale": age > 2 * 3600,
    }


def _fetch_quote(ticker: str) -> tuple[str, dict] | None:
    quote = None
    try:
        quote = _fetch_quote_yahoo(ticker)
    except Exception as e:
        print(f"  [warn] yfinance quote failed for {ticker}: {e}", file=sys.stderr)
    if quote is None:
        try:
            quote = _fetch_quote_direct(ticker)
            if quote:
                print(f"  [info] {ticker}: used direct chart-API fallback", file=sys.stderr)
        except Exception as e:
            print(f"  [warn] Direct chart-API fallback failed for {ticker}: {e}", file=sys.stderr)
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
        if "post_pct" in q:
            change = f"day {q['day_pct']:+.2f}%, AH {q['post_pct']:+.2f}%"
        else:
            change = f"{q['change_pct']:+.2f}%"
        tags = " <i>(last trade &gt;2h ago)</i>" if q.get("stale") else ""
        lines.append(f"{arrow} {html.escape(t)}: {q['price']:,.2f} ({change}){tags}")
    return "\n".join(lines)
