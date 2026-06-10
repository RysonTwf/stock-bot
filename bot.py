import os
import sys
from datetime import datetime

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
            prev_close = fi.previous_close
            change_pct = (current - prev_close) / prev_close * 100 if prev_close else 0.0
            data[name] = {"price": float(current), "change_pct": float(change_pct)}
        except Exception as e:
            print(f"  [warn] Could not fetch {ticker}: {e}", file=sys.stderr)
            data[name] = {"price": 0.0, "change_pct": 0.0}
    return data


def get_watchlist_moves() -> dict[str, float]:
    """Batch-fetch today's % change for all watchlist tickers."""
    try:
        raw    = yf.download(WATCHLIST, period="5d", progress=False, auto_adjust=True)
        closes = raw["Close"].dropna(how="all")
        if len(closes) < 2:
            return {}
        pct = (closes.iloc[-1] - closes.iloc[-2]) / closes.iloc[-2] * 100
        return {
            t: round(float(pct[t]), 2)
            for t in WATCHLIST
            if t in pct.index and pd.notna(pct[t])
        }
    except Exception as e:
        print(f"  [warn] Could not fetch watchlist moves: {e}", file=sys.stderr)
        return {}


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

    seen: set[str] = set()
    relevant: list[dict] = []
    fallback: list[dict] = []

    for h in all_headlines:
        title_lower = " " + h["title"].lower() + " "
        if h["title"] in seen:
            continue
        seen.add(h["title"])
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

    # Top movers from watchlist
    sorted_moves = sorted(ticker_moves.items(), key=lambda x: x[1])
    gainers = sorted_moves[-3:][::-1]
    losers  = sorted_moves[:3]
    gainers_str = " | ".join(f"{t} {p:+.1f}%" for t, p in gainers)
    losers_str  = " | ".join(f"{t} {p:+.1f}%" for t, p in losers)

    # Price lookup table for headline annotation
    price_ctx = " | ".join(
        f"{t} {p:+.1f}%"
        for t, p in sorted(ticker_moves.items())
    )

    headline_lines = "\n".join(
        f"  {i+1}. {h['title']}" for i, h in enumerate(headlines)
    )
    today = datetime.utcnow().strftime("%A, %B %d %Y")

    return f"""You are a sharp financial analyst writing the evening market brief for {today}.
Output a Telegram HTML message — no preamble, start directly with section 1.

MARKET DATA (indices):
{index_lines}

SECTOR ETFs:
{sector_lines}

{vix_line}

WATCHLIST TOP MOVERS TODAY:
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

SECTION 2 — <b>📈 Watchlist Movers</b>
- Top 3 gainers on one line, top 3 losers on one line, exactly as provided in WATCHLIST TOP MOVERS above.
- Format: ▲ TICKER +X.X% | TICKER +X.X% | TICKER +X.X%
- Format: ▼ TICKER -X.X% | TICKER -X.X% | TICKER -X.X%
- One closing sentence noting whether semis or tech led/lagged today overall.

SECTION 3 — <b>🔬 Semis + AI Headlines</b>
STRICT FILTERING RULES:
  • INCLUDE a headline if it names a specific company.
  • REJECT only headlines that name NO specific company at all.
  • Include every headline that passes, up to 10.
  • If ZERO pass, write: <i>No strong catalysts found today.</i>

For each headline that passes, write exactly one line:
📌 <b>Company (TICKER, DAY_MOVE):</b> [what happened] — [why it matters for the stock]
  - DAY_MOVE: look up the ticker in PRICE DATA above and append as e.g. +4.2% or -1.8%. If not found, omit.
  - Keep the explanation after the dash under 20 words.

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


def call_groq(prompt: str) -> str:
    client = Groq(api_key=GROQ_API_KEY)
    resp = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
        max_tokens=1800,
    )
    return resp.choices[0].message.content.strip()


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def send_telegram(message: str, chat_id: str | None = None) -> dict:
    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":                  chat_id or TELEGRAM_CHAT_ID,
        "text":                     message,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    ts = datetime.utcnow().isoformat(timespec="seconds")
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
    main()
