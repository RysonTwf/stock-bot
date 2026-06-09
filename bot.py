import os
import sys
from datetime import datetime

import feedparser
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
    for name, ticker in INDICES.items():
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="2d")
            if len(hist) >= 2:
                prev_close = hist["Close"].iloc[-2]
                current    = hist["Close"].iloc[-1]
                change_pct = (current - prev_close) / prev_close * 100
            elif len(hist) == 1:
                current    = hist["Close"].iloc[-1]
                change_pct = 0.0
            else:
                current    = 0.0
                change_pct = 0.0
            data[name] = {"price": float(current), "change_pct": float(change_pct)}
        except Exception as e:
            print(f"  [warn] Could not fetch {ticker}: {e}", file=sys.stderr)
            data[name] = {"price": 0.0, "change_pct": 0.0}
    return data


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

    # Prefer headlines that mention semiconductors or AI
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

    combined = relevant + fallback
    return combined[:top_n]


# ---------------------------------------------------------------------------
# Groq
# ---------------------------------------------------------------------------
def build_prompt(market_data: dict, headlines: list[dict]) -> str:
    market_lines = "\n".join(
        f"  {name}: ${d['price']:,.2f} ({d['change_pct']:+.2f}%)"
        for name, d in market_data.items()
    )
    headline_lines = "\n".join(
        f"  {i+1}. {h['title']}" for i, h in enumerate(headlines)
    )
    today = datetime.utcnow().strftime("%A, %B %d %Y")

    return f"""You are a sharp financial analyst writing the evening market brief for {today}.
Output a Telegram HTML message — no preamble, start directly with section 1.

MARKET DATA:
{market_lines}

RAW HEADLINES (may include noise — you must filter):
{headline_lines}

---

SECTION 1 — <b>📊 Market Pulse</b>
- One line per index: name, price, % change. Use ▲ for positive, ▼ for negative.
- End with one punchy sentence on overall market mood.

SECTION 2 — <b>🔬 Semis + AI Headlines</b>
STRICT FILTERING RULES — apply before writing anything:
  • INCLUDE a headline if it names a specific company — even if the ticker is not in the list below, use your knowledge to identify it.
    Reference tickers: NVDA, AMD, INTC, MU, MRVL, AMAT, ASML, QCOM, AVGO, TSM, TSEM, AAPL, MSFT, GOOGL, META, AMZN, TSLA, ORCL, ARM, LRCX, KLAC, TXN, ADI, NXPI, ON, MCHP, SMCI, DELL, HPE, CDNS, SNPS, SWKS.
  • Do NOT skip a headline just because the ticker is not in that list — if you know the company's ticker, include it.
  • REJECT only headlines that name NO specific company at all (e.g. "AI stocks could rise", "semiconductor sector faces headwinds").
  • You will receive up to 20 raw headlines. Include every one that names a specific company, up to 10 in the output.
  • If fewer than 3 headlines pass the filter, include only the ones that do.
  • If ZERO headlines pass the filter, write exactly: <i>No strong catalysts found today.</i>

For each headline that passes, write exactly one line in this format:
📌 <b>Company (TICKER):</b> [what happened] — [why it matters for the stock, in plain English]
Keep each line under 20 words after the dash.

SECTION 3 — <b>👀 One Thing To Watch</b>
- Pick the single highest-conviction catalyst from section 2.
- Must include: the company name, its ticker, a specific price or % move if available from the market data or headlines.
- Must give a concrete, specific reason why it matters TODAY — not a generic "AI is a tailwind" take.
- If section 2 had no strong catalysts, write: <i>No single standout catalyst today — wait for tomorrow's open.</i>
- Max 3 sentences.

---

FORMATTING RULES:
- Telegram HTML only: <b>bold</b>, <i>italic</i> — NO asterisks, NO markdown, NO ** syntax
- Tone: direct, like a friend who trades — not a press release
- Total message under 3800 characters
"""


def call_groq(prompt: str) -> str:
    client = Groq(api_key=GROQ_API_KEY)
    resp = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
        max_tokens=1500,
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
        print(f"    {name}: ${d['price']:,.2f} ({d['change_pct']:+.2f}%)")

    print("  Fetching RSS headlines...")
    headlines = get_headlines()
    for h in headlines:
        print(f"    - {h['title'][:80]}")

    print("  Calling Groq (llama-3.3-70b-versatile)...")
    prompt = build_prompt(market_data, headlines)
    brief  = call_groq(prompt)
    print(f"  Brief length: {len(brief)} chars")

    print("  Sending to Telegram...")
    result = send_telegram(brief)
    msg_id = result.get("result", {}).get("message_id", "?")
    print(f"  Done. Telegram message_id={msg_id}")


if __name__ == "__main__":
    main()
