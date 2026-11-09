import html
import os
import re
import sys
import time
from datetime import datetime, timezone

import feedparser
import requests
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
    """Live price + % change vs last completed close (any session) per index/ETF.

    Same quote source as movers and the watchlist (watchlist.get_live_quotes),
    so every section of the brief shares one session-aware baseline. Cash
    indices don't trade pre-market, so until the open they show the last close
    vs the close before it — same numbers the old daily-bars version produced.
    """
    tickers = {**INDICES, **SECTORS}
    quotes = get_live_quotes(list(tickers.values()))
    return {name: q for name, sym in tickers.items() if (q := quotes.get(sym))}


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
# Listicles, buy/sell advice and opinion questions (mostly Motley Fool-style
# syndication on the Yahoo feeds) crowd out actual news.
LOW_VALUE_RE = re.compile(
    r"\bstocks? to (buy|sell|own|hold|watch)\b"
    r"|\bprice prediction\b"
    r"|\bshould you\b"
    r"|\bis it too late\b"
    r"|\bunstoppable\b"
    r"|\byour (capital|money|portfolio)\b"
    r"|\bthis is the\b|\bwith this\b|\bthis respected\b|\bact now\b"
    r"|\bstocks? that explain\b"
    r"|\bbuy, sell,? or hold\b"
    r"|^\d+\s+(\w+\s+){0,4}(stocks?|reasons?|things|ways)\b"
    r"|\?\s*$",
    re.IGNORECASE,
)


LOW_VALUE_SOURCES = ("fool.com",)  # syndicated opinion and teaser headlines


def is_low_value_headline(title: str, link: str = "") -> bool:
    return (
        "cramer" in title.lower()
        or any(src in link for src in LOW_VALUE_SOURCES)
        or bool(LOW_VALUE_RE.search(title))
    )

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
        if is_low_value_headline(h["title"], h["link"]):
            continue
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
    lines = ["<b>📊 Market Pulse</b>"]
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
    lines.append("<b>📈 Movers</b>")
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


BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


def clean_llm_html(text: str) -> str:
    """Replace <br> with newlines: Telegram's HTML mode rejects the tag outright."""
    return BR_RE.sub("\n", text)


SOURCE_REF_RE = re.compile(r"\s*\[(\d+)\]")


def link_sources(text: str, headlines: list[dict]) -> str:
    """Turn the LLM's [n] headline references into tappable links to the article.

    Links are filled in from the fetched feed in Python, never written by the LLM.
    """
    def fix(m: re.Match) -> str:
        i = int(m.group(1)) - 1
        link = headlines[i]["link"] if 0 <= i < len(headlines) else ""
        return f' <a href="{html.escape(link, quote=True)}">↗</a>' if link else ""
    return SOURCE_REF_RE.sub(fix, text)


BIG_PICTURE_HEADER = "<b>🧭 Big Picture</b>"
HEADLINES_HEADER   = "<b>🔬 Semis + AI Headlines</b>"


def split_big_picture(text: str) -> tuple[str, str]:
    """Split the LLM output into (Big Picture, rest) so the summary can lead the brief."""
    if not text.startswith(BIG_PICTURE_HEADER) or HEADLINES_HEADER not in text:
        return "", text
    big, rest = text.split(HEADLINES_HEADER, 1)
    return big.strip(), HEADLINES_HEADER + rest


def build_prompt(headlines: list[dict], ticker_moves: dict[str, float], market_data: dict) -> str:
    market_ctx = " | ".join(
        f"{name} {d['change_pct']:+.1f}%" for name, d in market_data.items()
    )
    price_ctx = " | ".join(
        f"{t} {p:+.1f}%"
        for t, p in sorted(ticker_moves.items())
    )

    headline_lines = "\n".join(
        f"  {i+1}. {h['title']}" for i, h in enumerate(headlines)
    )
    today = datetime.now(timezone.utc).strftime("%A, %B %d %Y")

    return f"""You are a sharp financial analyst writing part of a market-open brief for {today}.
Output a Telegram HTML message — no preamble, start directly with the Big Picture section.

MARKET DATA (live % vs prev close — context only):
{market_ctx}

PRICE DATA (live % vs prev close — use to annotate headlines, do not list separately):
{price_ctx}

RAW HEADLINES (may include noise — you must filter):
{headline_lines}

---

SECTION — <b>🧭 Big Picture</b>
- Exactly 2 sentences on what is driving tech and chip stocks today, using MARKET DATA and the headlines.
- Do NOT write any numbers or percentages here — the brief shows the exact figures elsewhere.

SECTION — <b>🔬 Semis + AI Headlines</b>
SELECTION RULES:
  • Only consider headlines that name a specific company.
  • Pick the 5 MOST IMPORTANT for someone following semis and AI — real news (deals, earnings, products, guidance, regulation, legal action, analyst rating changes).
  • SKIP opinion and commentary pieces (e.g. "X is a triple threat", "Wall Street may be underestimating X") and multi-company roundups.
  • Fewer than 5 is fine if the rest are weak. If none qualify, write: <i>No strong catalysts found today.</i>

For each pick, write exactly one line using this EXACT format — no exceptions:
📌 <b>Company (TICKER, MOVE):</b> [what happened] — [why it matters] [N]
  - Company: the company's common name, e.g. Nvidia, Micron, TSMC — never the ticker.
  - TICKER: the stock ticker symbol, e.g. NVDA, MU, TSM
  - MOVE: find the ticker in PRICE DATA and copy its % exactly. If the ticker is not in PRICE DATA write N/A.
  - NEVER omit TICKER or MOVE. NEVER write "no move". Always write N/A if data is missing.
  - N: the number of the headline in RAW HEADLINES, in square brackets, e.g. [3]. Always include it.
  - Keep the explanation after the dash under 20 words.
  - ACCURACY RULES (highest priority):
    • Do NOT invert buy/sell/upgrade/downgrade direction. Copy the action word from the headline exactly.
    • Do NOT add facts that are not in the headline. No speculation about reasons or amounts.
    • The company MUST be named in that headline. Never attach a headline to a company it does not mention.
    • Never infer a direction (upgraded/downgraded, beat/missed, rose/fell) unless the headline states it.
    • If a headline is ambiguous, restate it conservatively rather than interpreting it.

SECTION — <b>👀 One Thing To Watch</b>
- Pick the single highest-conviction catalyst from the headlines section.
- Name the company and ticker with its move in the same (TICKER, MOVE) format, and say why it matters TODAY using only facts from the headline.
- If there were no catalysts: <i>No single standout catalyst today.</i>
- Max 3 sentences.

---

FORMATTING RULES:
- Telegram HTML only: <b>bold</b>, <i>italic</i> — NO asterisks, NO markdown, NO <br> (use plain line breaks)
- Tone: direct, like a friend who trades
- Total output under 2500 characters
"""


# 120b over 20b: the 20b pinned stories on companies the headline never named
# and guessed analyst-rating directions. Same free-tier limits on Groq.
GROQ_MODEL = "openai/gpt-oss-120b"


def call_groq(prompt: str, retries: int = 3) -> str:
    client = Groq(api_key=GROQ_API_KEY)
    for attempt in range(1, retries + 1):
        try:
            resp = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1800,
                reasoning_effort="low",
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
def build_brief() -> str:
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
        watchlist_section = format_watchlist(user_watchlist, quotes, show_session=False) + "\n\n"
        for t in user_watchlist:
            q = quotes.get(t)
            print(f"    {t}: {q['price']:,.2f} ({q['change_pct']:+.2f}%)" if q else f"    {t}: no data")

    print("  Fetching RSS headlines...")
    headlines = get_headlines()
    for h in headlines:
        print(f"    - {h['title'][:80]}")

    print(f"  Calling Groq ({GROQ_MODEL})...")
    prompt   = build_prompt(headlines, ticker_moves, market_data)
    llm_part = link_sources(enforce_annotations(clean_llm_html(call_groq(prompt)), ticker_moves), headlines)
    big_picture, llm_rest = split_big_picture(llm_part)

    # Market, movers and watchlist are rendered in Python so the numbers can't be garbled
    session_line = f"<i>US market: {market_session()} · all moves vs prev close</i>"
    brief = "\n\n".join(
        part for part in (
            session_line,
            big_picture,
            build_market_sections(market_data, ticker_moves),
            watchlist_section.strip(),
            llm_rest,
        ) if part
    )
    print(f"  Brief length: {len(brief)} chars")
    return brief


def main() -> None:
    brief = build_brief()
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
