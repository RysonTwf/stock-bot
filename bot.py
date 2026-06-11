import html
import os
import re
import sys
import time
from datetime import datetime, date, timezone

import feedparser
import requests
import yfinance as yf
from groq import Groq

from watchlist import load_watchlist, get_live_quotes, format_watchlist, market_session

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

# Universe scanned for the Premarket Movers section (distinct from the
# user-managed shared watchlist in watchlist.json)
MOVERS_UNIVERSE = [
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
    """Last completed session close + % change vs prior close, from daily bars.

    Daily bars match published closing values; fast_info is cached and has
    returned wrong VIX/index numbers.
    """
    tickers = {**INDICES, **SECTORS}
    data: dict = {}
    try:
        daily  = yf.download(list(tickers.values()), period="10d", interval="1d",
                             progress=False, auto_adjust=False)
        closes = daily["Close"]
    except Exception as e:
        print(f"  [warn] Could not fetch index data: {e}", file=sys.stderr)
        return data
    for name, ticker in tickers.items():
        try:
            s = closes[ticker].dropna()
            # Exclude any partial bar for today so we only report settled closes
            s = s[s.index.normalize().date < date.today()]
            if len(s) < 2:
                continue
            cur, prev = float(s.iloc[-1]), float(s.iloc[-2])
            data[name] = {"price": cur, "change_pct": (cur - prev) / prev * 100}
        except Exception as e:
            print(f"  [warn] Could not parse {ticker}: {e}", file=sys.stderr)
    return data


def get_universe_moves() -> dict[str, float]:
    """Live % change vs prev close (any session: pre/regular/post) per ticker.

    Same quote source as the watchlist, so annotations and movers always
    match what /watchlist shows.
    """
    moves: dict[str, float] = {}
    for ticker, q in get_live_quotes(MOVERS_UNIVERSE).items():
        if q["stale"]:
            continue  # last trade >2h old — no live session for this ticker
        val = round(q["change_pct"], 2)
        if abs(val) > 25:
            print(f"  [warn] {ticker} move {val:+.1f}% looks like bad data — skipping", file=sys.stderr)
            continue
        moves[ticker] = val
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
# Deterministic sections — numbers come straight from fetched data, never the LLM
# ---------------------------------------------------------------------------
def _vix_mood(val: float) -> str:
    return (
        "calm"     if val < 15 else
        "moderate" if val < 25 else
        "elevated" if val < 35 else
        "fearful"
    )


def _arrow(pct: float) -> str:
    return "▲" if pct >= 0 else "▼"


def build_market_sections(market_data: dict, ticker_moves: dict[str, float]) -> str:
    """Render Sections 1–2 in Python so every number is exactly what we fetched."""
    lines = ["<b>📊 Market Pulse</b> <i>(last US close)</i>"]
    for name in INDICES:
        d = market_data.get(name)
        if d:
            lines.append(f"{_arrow(d['change_pct'])} {html.escape(name)}: {d['price']:,.2f} ({d['change_pct']:+.2f}%)")

    etf_parts = [
        f"{name} {_arrow(d['change_pct'])} {d['change_pct']:+.2f}%"
        for name in ("SMH", "XLK")
        if (d := market_data.get(name))
    ]
    if etf_parts:
        lines.append(" | ".join(etf_parts))

    vix = market_data.get("VIX")
    if vix:
        lines.append(f"VIX: {vix['price']:.1f} ({vix['change_pct']:+.1f}%) — {_vix_mood(vix['price'])}")

    lines.append("")
    lines.append(f"<b>📈 Movers</b> <i>({market_session()}, vs prev close)</i>")
    sorted_moves = sorted(ticker_moves.items(), key=lambda x: x[1])
    gainers = [(t, p) for t, p in reversed(sorted_moves) if p > 0][:3]
    losers  = [(t, p) for t, p in sorted_moves if p < 0][:3]
    lines.append("▲ " + (" | ".join(f"{t} {p:+.1f}%" for t, p in gainers) or "<i>no gainers</i>"))
    lines.append("▼ " + (" | ".join(f"{t} {p:+.1f}%" for t, p in losers) or "<i>no losers</i>"))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Groq — only summarizes headlines; all numbers get overwritten afterwards
# ---------------------------------------------------------------------------
ANNOTATION_RE = re.compile(r"\(([A-Z]{1,5}),\s*(?:[+-]?\d+(?:[.,]\d+)?\s*%|N/?A|n/a)\)")


def enforce_annotations(text: str, ticker_moves: dict[str, float]) -> str:
    """Overwrite every (TICKER, ±X.X%) the LLM wrote with the real fetched number.

    The LLM is given the price data but still garbles or invents figures;
    this makes the printed numbers trustworthy regardless.
    """
    def fix(m: re.Match) -> str:
        t = m.group(1)
        if t in ticker_moves:
            return f"({t}, {ticker_moves[t]:+.1f}%)"
        return f"({t}, N/A)"
    return ANNOTATION_RE.sub(fix, text)


def build_prompt(headlines: list[dict], ticker_moves: dict[str, float]) -> str:
    price_ctx = " | ".join(
        f"{t} {p:+.1f}%"
        for t, p in sorted(ticker_moves.items())
    )

    headline_lines = "\n".join(
        f"  {i+1}. {h['title']}" for i, h in enumerate(headlines)
    )
    today = datetime.now(timezone.utc).strftime("%A, %B %d %Y")

    return f"""You are a sharp financial analyst writing the headlines portion of a premarket brief for {today}.
Output a Telegram HTML message — no preamble, no market summary, start directly with the headlines section.

PRICE DATA (live % vs prev close — use to annotate headlines, do not list separately):
{price_ctx}

RAW HEADLINES (may include noise — you must filter):
{headline_lines}

---

SECTION — <b>🔬 Semis + AI Headlines</b>
STRICT FILTERING RULES:
  • INCLUDE a headline if it names a specific company.
  • REJECT only headlines that name NO specific company at all.
  • Include every headline that passes, up to 10.
  • If ZERO pass, write: <i>No strong catalysts found today.</i>

For each headline that passes, write exactly one line using this EXACT format — no exceptions:
📌 <b>Company (TICKER, MOVE):</b> [what happened] — [why it matters]
  - TICKER: the stock ticker symbol, e.g. NVDA, MU, TSM
  - MOVE: find the ticker in PRICE DATA and copy its % exactly. If the ticker is not in PRICE DATA write N/A.
  - NEVER omit TICKER or MOVE. NEVER write "no move". Always write N/A if data is missing.
  - Keep the explanation after the dash under 20 words.
  - ACCURACY RULES (highest priority):
    • Do NOT invert buy/sell/upgrade/downgrade direction. Copy the action word from the headline exactly.
    • Do NOT add facts that are not in the headline. No speculation about reasons or amounts.
    • If a headline is ambiguous, restate it conservatively rather than interpreting it.

SECTION — <b>👀 One Thing To Watch</b>
- Pick the single highest-conviction catalyst from the headlines section.
- Name the company and ticker with its move in the same (TICKER, MOVE) format, and say why it matters TODAY using only facts from the headline.
- If there were no catalysts: <i>No single standout catalyst today — wait for the open.</i>
- Max 3 sentences.

---

FORMATTING RULES:
- Telegram HTML only: <b>bold</b>, <i>italic</i> — NO asterisks, NO markdown
- Tone: direct, like a friend who trades
- Total output under 2500 characters
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

    print("  Fetching live movers...")
    ticker_moves = get_universe_moves()
    sorted_moves = sorted(ticker_moves.items(), key=lambda x: x[1])
    for t, p in sorted_moves[-3:][::-1] + sorted_moves[:3]:
        print(f"    {t}: {p:+.2f}%")

    print("  Fetching shared watchlist quotes...")
    user_watchlist = load_watchlist()
    watchlist_section = ""
    if user_watchlist:
        quotes = get_live_quotes(user_watchlist)
        watchlist_section = format_watchlist(user_watchlist, quotes) + "\n\n"
        for t in user_watchlist:
            q = quotes.get(t)
            print(f"    {t}: {q['price']:,.2f} ({q['change_pct']:+.2f}%)" if q else f"    {t}: no data")

    print("  Fetching RSS headlines...")
    headlines = get_headlines()
    for h in headlines:
        print(f"    - {h['title'][:80]}")

    print("  Calling Groq (llama-3.3-70b-versatile)...")
    prompt   = build_prompt(headlines, ticker_moves)
    llm_part = enforce_annotations(call_groq(prompt), ticker_moves)

    # Sections 1–3 are rendered in Python so the numbers can't be garbled
    brief = build_market_sections(market_data, ticker_moves) + "\n\n" + watchlist_section + llm_part
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
