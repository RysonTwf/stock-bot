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

# Fixed scan universe for ticker-price annotations on headlines/chatter
# (distinct from the user-managed shared watchlist in watchlist.json)
MOVERS_UNIVERSE = [
    "NVDA", "AMD",  "MU",   "MRVL", "INTC", "AMAT",
    "AAPL", "MSFT", "GOOGL","META", "AMZN", "TSLA", "ORCL", "ARM", "AVGO", "QCOM",
    "TSM",  "ASML", "LRCX", "KLAC", "TSEM", "TXN",  "SWKS", "ONTO", "WOLF", "SLAB",
    "SMCI", "HPE",  "DELL", "CDNS", "SNPS", "MCHP", "ADI",  "NXPI", "ON",   "STM",
]

REDDIT_SUBS = ["wallstreetbets", "stocks", "investing"]

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

    Same quote source as the watchlist, so headline/chatter annotations
    always match what /watchlist shows.
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


def get_trending_symbols(top_n: int = 6) -> list[str]:
    """Tickers currently trending on StockTwits — a live read of retail
    attention across the whole platform, independent of MOVERS_UNIVERSE.

    Public endpoint, no API key needed.
    """
    try:
        resp = requests.get(
            "https://api.stocktwits.com/api/2/trending/symbols.json",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        resp.raise_for_status()
        symbols = resp.json().get("symbols", [])
    except Exception as e:
        print(f"  [warn] StockTwits trending fetch failed: {e}", file=sys.stderr)
        return []
    return [s["symbol"] for s in symbols if s.get("symbol")][:top_n]


def get_stocktwits_buzz(tickers: list[str], per_ticker: int = 8) -> dict[str, list[str]]:
    """Raw recent retail posts per ticker from StockTwits' public stream API
    (no API key needed). Fed to Groq for summarization in build_prompt() —
    never shown verbatim, since individual posts are noisy and unverified.
    """
    buzz: dict[str, list[str]] = {}
    for t in tickers:
        try:
            resp = requests.get(
                f"https://api.stocktwits.com/api/2/streams/symbol/{t}.json",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            resp.raise_for_status()
            messages = resp.json().get("messages", [])
        except Exception as e:
            print(f"  [warn] StockTwits chatter fetch failed for {t}: {e}", file=sys.stderr)
            continue
        bodies = [m["body"].strip() for m in messages if m.get("body", "").strip()][:per_ticker]
        if bodies:
            buzz[t] = bodies
    return buzz


# ---------------------------------------------------------------------------
# Reddit posts (via PRAW — official API, not blocked by Reddit)
# ---------------------------------------------------------------------------
def get_reddit_posts(top_n: int = 5) -> list[dict]:
    """Fetch hot posts from finance subreddits via PRAW (Reddit official API).

    Requires REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET env vars (read-only
    script app registered at reddit.com/prefs/apps). Silently returns [] if
    credentials are absent so the section is just skipped in dev without them.
    """
    import praw  # optional dependency — only imported when credentials exist

    client_id     = os.environ.get("REDDIT_CLIENT_ID", "")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        print("  [warn] Reddit: REDDIT_CLIENT_ID/SECRET not set — skipping", file=sys.stderr)
        return []

    reddit = praw.Reddit(
        client_id=client_id,
        client_secret=client_secret,
        user_agent="stockbot/1.0 by RysonTwf",
    )

    posts: list[dict] = []
    for sub in REDDIT_SUBS:
        try:
            for post in reddit.subreddit(sub).hot(limit=25):
                if post.stickied:
                    continue
                posts.append({
                    "title": post.title.strip(),
                    "url":   f"https://reddit.com{post.permalink}",
                    "score": post.score,
                    "sub":   sub,
                })
        except Exception as e:
            print(f"  [warn] Reddit r/{sub}: {e}", file=sys.stderr)

    def _words(title: str) -> set[str]:
        return set(re.findall(r"[a-z]{3,}", title.lower()))

    kept_words: list[set[str]] = []
    relevant: list[dict] = []
    fallback: list[dict] = []

    for p in sorted(posts, key=lambda x: x["score"], reverse=True):
        title_lower = " " + p["title"].lower() + " "
        words = _words(p["title"])
        if words and any(len(words & kw) / len(words | kw) > 0.6 for kw in kept_words):
            continue
        kept_words.append(words)
        if any(kw in title_lower for kw in SEMI_AI_KEYWORDS):
            relevant.append(p)
        else:
            fallback.append(p)

    return (relevant + fallback)[:top_n]


def build_reddit_section(posts: list[dict]) -> str:
    if not posts:
        return ""
    lines = ["<b>📰 Reddit Buzz</b> <i>(wsb · stocks · investing)</i>"]
    for p in posts:
        title = html.escape(p["title"])
        lines.append(f'• <a href="{p["url"]}">{title}</a> <i>(r/{p["sub"]}, ↑{p["score"]:,})</i>')
    return "\n".join(lines)


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


def build_market_sections(market_data: dict, trending: list[tuple[str, float | None]]) -> str:
    """Render Sections 1–2 in Python so every number is exactly what we fetched."""
    lines = [f"<b>📊 Market Pulse</b> <i>({market_session()}, vs prev close)</i>"]
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
    lines.append(f"<b>🔥 Trending Now</b> <i>({market_session()}, vs prev close)</i> <i>(StockTwits)</i>")
    if trending:
        lines.append(" | ".join(f"{t} {pct:+.1f}%" if pct is not None else t for t, pct in trending))
    else:
        lines.append("<i>no trending data</i>")
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


def label_llm_sections(text: str) -> str:
    """Append the session label to the LLM-written section headers in Python,
    so it's always present and exact rather than trusting the LLM to copy it."""
    label = f" <i>({market_session()}, vs prev close)</i>"
    for header in (
        "<b>🔬 Semis + AI Headlines</b>",
        "<b>💬 Retail Chatter</b>",
        "<b>👀 One Thing To Watch</b>",
    ):
        text = text.replace(header, header + label, 1)
    return text


def build_prompt(
    headlines: list[dict],
    ticker_moves: dict[str, float],
    retail_buzz: dict[str, list[str]],
) -> str:
    price_ctx = " | ".join(
        f"{t} {p:+.1f}%"
        for t, p in sorted(ticker_moves.items())
    )

    headline_lines = "\n".join(
        f"  {i+1}. {h['title']}" for i, h in enumerate(headlines)
    )

    chatter_lines = "\n".join(
        f"  {t}:\n" + "\n".join(f"    - {body}" for body in bodies)
        for t, bodies in retail_buzz.items()
    ) or "  (none fetched today)"

    today = datetime.now(timezone.utc).strftime("%A, %B %d %Y")

    return f"""You are a sharp financial analyst writing the headlines and retail-sentiment portions of a premarket brief for {today}.
Output a Telegram HTML message — no preamble, no market summary, start directly with the headlines section.

PRICE DATA (live % vs prev close — use to annotate headlines/chatter, do not list separately):
{price_ctx}

RAW HEADLINES (may include noise — you must filter):
{headline_lines}

RETAIL CHATTER (raw StockTwits posts per watchlist ticker — ordinary retail traders talking, NOT news. Summarize sentiment/themes only; do not quote verbatim and do not treat anything in it as verified fact):
{chatter_lines}

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

SECTION — <b>💬 Retail Chatter</b>
- For each ticker in RETAIL CHATTER that has posts listed, write exactly one line:
  🗣️ <b>TICKER (MOVE):</b> [one short sentence: overall sentiment (bullish/bearish/mixed) + the main theme or concern people are raising]
  - MOVE: copy the ticker's % from PRICE DATA if present, else N/A.
  - Do not invent specific facts, numbers, or news that aren't reflected in the posts — this is vibes/sentiment, not reporting.
  - Skip any ticker with no posts listed — do not write a line for it.
- If RETAIL CHATTER says "(none fetched today)" or every ticker has no posts, write exactly: <i>No notable retail chatter today.</i>

SECTION — <b>👀 One Thing To Watch</b>
- Pick the single highest-conviction catalyst from the headlines section (prefer headlines over chatter).
- Name the company and ticker with its move in the same (TICKER, MOVE) format, and say why it matters TODAY using only facts from the headline.
- If there were no catalysts: <i>No single standout catalyst today — wait for the open.</i>
- Max 3 sentences.

---

FORMATTING RULES:
- Telegram HTML only: <b>bold</b>, <i>italic</i> — NO asterisks, NO markdown
- Tone: direct, like a friend who trades
- Total output under 3000 characters
"""


def call_groq(prompt: str, retries: int = 3) -> str:
    client = Groq(api_key=GROQ_API_KEY)
    for attempt in range(1, retries + 1):
        try:
            resp = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=2200,
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

    print("  Fetching trending symbols...")
    trending_symbols = get_trending_symbols()
    trending_quotes = get_live_quotes(trending_symbols) if trending_symbols else {}
    trending = [(t, trending_quotes[t]["change_pct"] if t in trending_quotes else None) for t in trending_symbols]
    for t, pct in trending:
        print(f"    {t}: {pct:+.2f}%" if pct is not None else f"    {t}: no data")

    print("  Fetching shared watchlist quotes...")
    user_watchlist = load_watchlist()
    watchlist_section = ""
    watchlist_quotes: dict[str, dict] = {}
    if user_watchlist:
        watchlist_quotes = get_live_quotes(user_watchlist)
        watchlist_section = format_watchlist(user_watchlist, watchlist_quotes) + "\n\n"
        for t in user_watchlist:
            q = watchlist_quotes.get(t)
            print(f"    {t}: {q['price']:,.2f} ({q['change_pct']:+.2f}%)" if q else f"    {t}: no data")

    # Merge the fixed scan universe with watchlist moves so headline/chatter
    # annotations can reference any watchlist ticker too, not just MOVERS_UNIVERSE.
    all_moves = dict(ticker_moves)
    all_moves.update({t: round(q["change_pct"], 2) for t, q in watchlist_quotes.items()})

    print("  Fetching StockTwits retail chatter...")
    retail_buzz = get_stocktwits_buzz(user_watchlist) if user_watchlist else {}
    for t, bodies in retail_buzz.items():
        print(f"    {t}: {len(bodies)} posts")

    print("  Fetching Reddit posts...")
    reddit_posts = get_reddit_posts()
    reddit_section = build_reddit_section(reddit_posts) + "\n\n" if reddit_posts else ""
    for p in reddit_posts:
        print(f"    r/{p['sub']}: {p['title'][:80]}")

    print("  Fetching RSS headlines...")
    headlines = get_headlines()
    for h in headlines:
        print(f"    - {h['title'][:80]}")

    print("  Calling Groq (llama-3.3-70b-versatile)...")
    prompt   = build_prompt(headlines, all_moves, retail_buzz)
    llm_part = label_llm_sections(enforce_annotations(call_groq(prompt), all_moves))

    # Market Pulse / Trending / Watchlist are rendered in Python so the numbers can't be garbled
    brief = build_market_sections(market_data, trending) + "\n\n" + watchlist_section + reddit_section + llm_part
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
