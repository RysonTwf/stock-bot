import html
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timezone

import feedparser
import pandas as pd
import requests
import yfinance as yf
from groq import Groq

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]

INDICES = {
    "S&P 500": "^GSPC",
    "Nasdaq":  "^IXIC",
    "Dow":     "^DJI",
}

SECTORS = {
    "SMH": "SMH",   # VanEck Semiconductor ETF
    "XLK": "XLK",   # Tech Select Sector SPDR
    "VIX": "^VIX",  # CBOE Volatility Index
}

WATCHLIST = [
    "NVDA", "AMD",  "MU",   "MRVL", "INTC", "AMAT",
    "AAPL", "MSFT", "GOOGL","META", "AMZN", "TSLA", "ORCL", "ARM", "AVGO", "QCOM",
    "TSM",  "ASML", "LRCX", "KLAC", "TSEM", "TXN",  "SWKS", "ONTO", "WOLF", "SLAB",
    "SMCI", "HPE",  "DELL", "CDNS", "SNPS", "MCHP", "ADI",  "NXPI", "ON",   "STM",
]

RSS_FEEDS = [
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=NVDA,AMD,MU,MRVL,INTC,AMAT&region=US&lang=en-US",
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=AAPL,MSFT,GOOGL,META,AMZN,TSLA,ORCL,ARM,AVGO,QCOM&region=US&lang=en-US",
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=TSM,ASML,LRCX,KLAC,TSEM,TXN,SWKS,ONTO,WOLF,SLAB&region=US&lang=en-US",
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=SMCI,HPE,DELL,CDNS,SNPS,MCHP,ADI,NXPI,ON,STM&region=US&lang=en-US",
    "https://www.cnbc.com/id/19854910/device/rss/rss.html",
    "https://www.cnbc.com/id/10000664/device/rss/rss.html",
]

SEMI_AI_KEYWORDS = [
    "semiconductor", "chip", "chips", "wafer", "foundry", "fab",
    "nvidia", "amd", "intel", "tsmc", "qualcomm", "broadcom",
    "micron", "marvell", "applied materials", "amat", "asml",
    "artificial intelligence", " ai ", "machine learning", "gpu",
    "datacenter", "data center", "big tech",
    "apple", "microsoft", "google", "alphabet", "meta", "amazon", "aws",
    "openai", "anthropic", "llm", "large language", "generative",
]


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------
def get_market_data() -> dict:
    data = {}
    for name, ticker in {**INDICES, **SECTORS}.items():
        try:
            fi         = yf.Ticker(ticker).fast_info
            current    = fi.last_price
            prev_close = fi.regular_market_previous_close  # previous_close is stale/wrong
            change_pct = (current - prev_close) / prev_close * 100 if prev_close else 0.0
            data[name] = {"price": float(current), "change_pct": float(change_pct)}
        except Exception as e:
            print(f"  [warn] Could not fetch {ticker}: {e}", file=sys.stderr)
            data[name] = {"price": 0.0, "change_pct": 0.0}
    return data


def get_watchlist_moves() -> dict[str, float]:
    """Return premarket % change vs previous close for each watchlist ticker."""
    # Step 1: batch-fetch previous closes (reliable with daily interval)
    try:
        daily = yf.download(WATCHLIST, period="5d", interval="1d",
                            progress=False, auto_adjust=True)
        closes = daily["Close"].dropna(how="all")
        # Drop any partial row for today so iloc[-1] is always the last completed session
        closes = closes[closes.index.normalize().date < date.today()]
        if closes.empty:
            print("  [warn] No completed daily session data", file=sys.stderr)
            return {}
        prev_closes = closes.iloc[-1]
    except Exception as e:
        print(f"  [warn] Could not fetch daily closes: {e}", file=sys.stderr)
        return {}

    # Step 2: per-ticker 1-min history with prepost — batch download misses many tickers
    def _fetch_current(ticker: str) -> tuple[str, float] | None:
        try:
            prev = float(prev_closes.get(ticker, float("nan")))
            if pd.isna(prev) or prev == 0:
                return None
            hist = yf.Ticker(ticker).history(period="1d", interval="1m", prepost=True)
            if hist.empty:
                return None
            curr = float(hist["Close"].iloc[-1])
            if pd.isna(curr):
                return None
            val = round((curr - prev) / prev * 100, 2)
            if abs(val) > 25:
                print(f"  [warn] {ticker} move {val:+.1f}% looks like bad data — skipping", file=sys.stderr)
                return None
            return ticker, val
        except Exception as e:
            print(f"  [warn] Could not fetch {ticker}: {e}", file=sys.stderr)
            return None

    moves: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        for fut in as_completed({ex.submit(_fetch_current, t): t for t in WATCHLIST}):
            try:
                item = fut.result()
                if item:
                    moves[item[0]] = item[1]
            except Exception as e:
                print(f"  [warn] Watchlist future failed: {e}", file=sys.stderr)
    return moves


# ---------------------------------------------------------------------------
# RSS headlines
# ---------------------------------------------------------------------------
def get_headlines(max_per_feed: int = 30, top_n: int = 20) -> list[dict]:
    all_headlines: list[dict] = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:max_per_feed]:
                title = entry.get("title", "").strip()
                link  = entry.get("link", "").strip()
                if title:
                    all_headlines.append({"title": title, "link": link})
        except Exception as e:
            print(f"  [warn] RSS fetch failed for {url}: {e}", file=sys.stderr)

    def _words(title: str) -> set[str]:
        return set(re.findall(r"[a-z]{3,}", title.lower()))

    kept_words: list[set[str]] = []
    relevant: list[dict] = []
    fallback: list[dict] = []

    for h in all_headlines:
        title_lower = " " + h["title"].lower() + " "
        words = _words(h["title"])
        # Skip near-duplicates: same story rewritten by another outlet
        if words and any(
            len(words & kw) / len(words | kw) > 0.6 for kw in kept_words
        ):
            continue
        kept_words.append(words)
        if any(kw in title_lower for kw in SEMI_AI_KEYWORDS):
            relevant.append(h)
        else:
            fallback.append(h)

    return (relevant + fallback)[:top_n]


# ---------------------------------------------------------------------------
# Groq
# ---------------------------------------------------------------------------
def build_prompt(market_data: dict, headlines: list[dict], ticker_moves: dict[str, float]) -> str:
    # Indices
    index_lines = "\n".join(
        f"  {name}: ${d['price']:,.2f} ({d['change_pct']:+.2f}%)"
        for name, d in market_data.items()
        if name in INDICES
    )

    # Sector ETFs
    sector_lines = "\n".join(
        f"  {name}: ${d['price']:,.2f} ({d['change_pct']:+.2f}%)"
        for name, d in market_data.items()
        if name in ("SMH", "XLK")
    )

    # VIX with mood label
    vix      = market_data.get("VIX", {})
    vix_val  = vix.get("price", 0.0)
    vix_pct  = vix.get("change_pct", 0.0)
    vix_mood = (
        "calm"     if vix_val < 15 else
        "moderate" if vix_val < 25 else
        "elevated" if vix_val < 35 else
        "fearful"
    )
    vix_line = f"  VIX: {vix_val:.1f} ({vix_pct:+.1f}%) — {vix_mood}"

    # Top movers from watchlist — only true gainers/losers, never a negative under ▲
    sorted_moves = sorted(ticker_moves.items(), key=lambda x: x[1])
    gainers = [(t, p) for t, p in reversed(sorted_moves) if p > 0][:3]
    losers  = [(t, p) for t, p in sorted_moves if p < 0][:3]
    gainers_str = " | ".join(f"{t} {p:+.1f}%" for t, p in gainers) or "none"
    losers_str  = " | ".join(f"{t} {p:+.1f}%" for t, p in losers) or "none"

    # Price lookup table for headline annotation
    price_ctx = " | ".join(
        f"{t} {p:+.1f}%"
        for t, p in sorted(ticker_moves.items())
    )

    headline_lines = "\n".join(
        f"  {i+1}. {h['title']}" for i, h in enumerate(headlines)
    )
    today = datetime.now(timezone.utc).strftime("%A, %B %d %Y")

    return f"""You are a sharp financial analyst writing the evening market brief for {today}.
Output a Telegram HTML message — no preamble, start directly with section 1.

MARKET DATA (indices):
{index_lines}

SECTOR ETFs:
{sector_lines}

{vix_line}

PREMARKET MOVERS (vs previous close):
  ▲ {gainers_str}
  ▼ {losers_str}

PRICE DATA (use to annotate headlines — do not list separately):
{price_ctx}

RAW HEADLINES (may include noise — you must filter):
{headline_lines}

---

SECTION 1 — <b>📊 Market Pulse</b>
- One line per index (S&P 500, Nasdaq, Dow): name, price, % change. Use ▲/▼.
- One line for sector ETFs: SMH and XLK with % change and ▲/▼.
- One line for VIX: value, % change, and the mood label ({vix_mood}).
- End with one punchy sentence on overall market mood.

SECTION 2 — <b>📈 Premarket Movers</b> <i>(vs prev close)</i>
- Top 3 gainers on one line, top 3 losers on one line, exactly as provided in PREMARKET MOVERS above.
- Format: ▲ TICKER +X.X% | TICKER +X.X% | TICKER +X.X%
- Format: ▼ TICKER -X.X% | TICKER -X.X% | TICKER -X.X%
- If a line says "none", write that line as: ▲ <i>no gainers in premarket</i> (or ▼ <i>no losers in premarket</i>).
- One closing sentence noting whether semis or tech are leading/lagging in premarket.

SECTION 3 — <b>🔬 Semis + AI Headlines</b>
STRICT FILTERING RULES:
  • INCLUDE a headline if it names a specific company.
  • REJECT only headlines that name NO specific company at all.
  • Include every headline that passes, up to 10.
  • If ZERO pass, write: <i>No strong catalysts found today.</i>

For each headline that passes, write exactly one line using this EXACT format — no exceptions:
📌 <b>Company (TICKER, MOVE):</b> [what happened] — [why it matters]
  - TICKER: the stock ticker symbol, e.g. NVDA, MU, TSM
  - MOVE: find the ticker in PRICE DATA and write the premarket % vs prev close, e.g. +4.2% or -1.8%. If the ticker is not in PRICE DATA write N/A.
  - NEVER omit TICKER or MOVE. NEVER write "no move". Always write N/A if data is missing.
  - Keep the explanation after the dash under 20 words.
  - CRITICAL: Do NOT invert or paraphrase buy/sell/upgrade/downgrade direction. If the headline says "bought", write bought. If it says "sold", write sold. Copy the action exactly.

SECTION 4 — <b>👀 One Thing To Watch</b>
- Pick the single highest-conviction catalyst from section 3.
- Must name the company, ticker, its day move from PRICE DATA, and a specific reason why it matters TODAY.
- If section 3 had no catalysts: <i>No single standout catalyst today — wait for tomorrow's open.</i>
- Max 3 sentences.

---

FORMATTING RULES:
- Telegram HTML only: <b>bold</b>, <i>italic</i> — NO asterisks, NO markdown
- Tone: direct, like a friend who trades
- Total message under 4000 characters
"""


def call_groq(prompt: str, retries: int = 3) -> str:
    client = Groq(api_key=GROQ_API_KEY)
    for attempt in range(1, retries + 1):
        try:
            resp = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=1800,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            if attempt == retries:
                raise
            print(f"  [warn] Groq attempt {attempt} failed: {e} — retrying...", file=sys.stderr)
            time.sleep(2 * attempt)
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def send_telegram(message: str, chat_id: str | None = None) -> dict:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    # Telegram hard limit is 4096 chars — truncate at a line break to keep HTML tags intact
    if len(message) > 4000:
        cut = message.rfind("\n", 0, 4000)
        message = message[: cut if cut > 0 else 4000] + "\n…"

    payload = {
        "chat_id":                  chat_id or TELEGRAM_CHAT_ID,
        "text":                     message,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=30)

    # LLM occasionally emits malformed HTML that Telegram rejects — degrade to plain text
    if resp.status_code == 400:
        print(f"  [warn] Telegram rejected HTML ({resp.text[:200]}) — retrying as plain text", file=sys.stderr)
        plain = re.sub(r"</?(b|i|u|s|a|code|pre)\b[^>]*>", "", message)
        payload = {
            "chat_id":                  chat_id or TELEGRAM_CHAT_ID,
            "text":                     html.unescape(plain),
            "disable_web_page_preview": True,
        }
        resp = requests.post(url, json=payload, timeout=30)

    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{ts}] Starting daily brief...")

    print("  Fetching market data...")
    market_data = get_market_data()
    for name, d in market_data.items():
        print(f"    {name}: {d['price']:,.2f} ({d['change_pct']:+.2f}%)")

    print("  Fetching watchlist moves...")
    ticker_moves = get_watchlist_moves()
    sorted_moves = sorted(ticker_moves.items(), key=lambda x: x[1])
    for t, p in sorted_moves[-3:][::-1] + sorted_moves[:3]:
        print(f"    {t}: {p:+.2f}%")

    print("  Fetching RSS headlines...")
    headlines = get_headlines()
    for h in headlines:
        print(f"    - {h['title'][:80]}")

    print("  Calling Groq (llama-3.3-70b-versatile)...")
    prompt = build_prompt(market_data, headlines, ticker_moves)
    brief  = call_groq(prompt)
    print(f"  Brief length: {len(brief)} chars")

    print("  Sending to Telegram...")
    result = send_telegram(brief)
    msg_id = result.get("result", {}).get("message_id", "?")
    print(f"  Done. Telegram message_id={msg_id}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Tell the chat the brief failed instead of silently sending nothing
        try:
            send_telegram(f"⚠️ Daily brief failed: {html.escape(str(e)[:300])}")
        except Exception:
            pass
        raise
