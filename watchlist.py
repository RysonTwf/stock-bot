"""Shared user watchlist — storage, live quotes (incl. pre/post market), formatting.

The list itself lives in watchlist.json at the repo root. app.py edits it via the
GitHub Contents API so changes persist; bot.py reads the checked-out file in Actions.
"""
import html
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
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


def _fetch_quote(ticker: str) -> tuple[str, dict] | None:
    try:
        tk = yf.Ticker(ticker)
        daily = tk.history(period="10d", interval="1d")["Close"].dropna()
        # Only settled closes: drop any bar for today (ET) so prev close is stable
        et_today = datetime.now(_ET).date()
        completed = daily[[ts.date() < et_today for ts in daily.index]]
        if completed.empty:
            return None
        prev = float(completed.iloc[-1])
        if prev == 0:
            return None

        hist = tk.history(period="1d", interval="1m", prepost=True)
        closes_1m = hist["Close"].dropna() if not hist.empty else hist
        if closes_1m.empty:
            return None
        age = pd.Timestamp.now(tz=closes_1m.index.tz) - closes_1m.index[-1]
        price = float(closes_1m.iloc[-1])
        return ticker, {
            "price": price,
            "change_pct": (price - prev) / prev * 100,
            "stale": age > pd.Timedelta(hours=2),
        }
    except Exception as e:
        print(f"  [warn] Quote fetch failed for {ticker}: {e}", file=sys.stderr)
        return None


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
        stale = " <i>(last trade &gt;2h ago)</i>" if q["stale"] else ""
        lines.append(f"{arrow} {html.escape(t)}: {q['price']:,.2f} ({q['change_pct']:+.2f}%){stale}")
    return "\n".join(lines)
