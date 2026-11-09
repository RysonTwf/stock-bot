"""Standalone tests for the brief's headline filter and LLM-output post-processing.

No pytest needed:  python tests/test_brief_format.py

Headline titles below are real ones the bot fetched in Oct 2026 runs.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# bot.py reads these at import time; the functions under test never use them
for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GROQ_API_KEY"):
    os.environ.setdefault(var, "test")

import bot

LOW_VALUE = [
    "1 Unstoppable Artificial Intelligence (AI) Stock to Buy Before It Joins Nvidia in the $5 Trillion Club",
    "Is AMD Stock Paying You Enough For The Swings?",
    "Alphabet Price Prediction: 56 Analysts Bullish, Zero Sells",
    "AI Capex Efficiency Should Guide Your Capital Into These Two Hyperscalers",
    "Are Samsung and TSMC's record revenues still not enough for the AI trade?",
    "3 Top Semiconductor Stocks to Own for the Next Decade",
    "Should You Buy Micron Stock Right Now?",
    "Is It Too Late to Buy Nvidia?",
    # Teaser-style titles from the 2026-11-09 feed
    "Not AMD. Not Intel. This Is the Artificial Intelligence (AI) Chip Stock Nvidia Should Fear Most.",
    "Nvidia, Palantir, and Tesla Are Sending Shockwaves Through Wall Street With This $53 Billion Warning",
    "AMD Shed Nearly 30% in 30 Days: This Respected Wall Street Team Expects 75% Gains For Those Who Act Now",
    "Nvidia, AMD, Intel, Berkshire Hathaway, and More Stocks That Explain Today's Market",
    "Buy, Sell, or Hold? Monday's Top Upgrades and Downgrades: Advanced Micro Devices, Airbnb, Nvidia, and More",
]

REAL_NEWS = [
    "NVIDIA Commits $1 Billion to Advance US Science Over the Next Five Years",
    "GlobalFoundries shares rise on $2 billion TSMC agreement",
    "Securitize Launches Tokenized Shares of Apple, Nvidia and Strategy",
    "Amazon targets Apple with high-end Alexa tablets featuring Google Play apps",
    "Morgan Stanley raises Nvidia price target to $300",
    "Micron Could Be Poised for a Major Breakout Late in 2026",
    "3M beats quarterly estimates",
    "Nvidia braces for $200bn hit over 'Chinese spy' affair",
    "Nvidia Board Launches Probe In CEO Jensen Huang Spying Case. The Stock Is Falling.",
    "Tech stocks claw back some losses despite Nvidia concerns",
    "Markets Rally as Nvidia Fears Subside and Rotation Gathers Pace",
]


def test_low_value_filter():
    for title in LOW_VALUE:
        assert bot.is_low_value_headline(title), f"should drop: {title}"
    for title in REAL_NEWS:
        assert not bot.is_low_value_headline(title), f"should keep: {title}"
    # Motley Fool is syndicated opinion and teasers: dropped by source, whatever the title
    assert bot.is_low_value_headline("Nvidia reports record revenue",
                                     "https://www.fool.com/investing/2026/11/09/x/?.tsrc=rss")
    assert not bot.is_low_value_headline("Nvidia reports record revenue",
                                         "https://finance.yahoo.com/news/x.html")


HEADLINES = [
    {"title": "NVIDIA Commits $1 Billion", "link": "https://example.com/nvda?a=1&b=2"},
    {"title": "GlobalFoundries shares rise", "link": "https://example.com/gfs"},
    {"title": "No link here", "link": ""},
]


def test_link_sources():
    text = ("📌 <b>NVIDIA (NVDA, +1.0%):</b> funds science — goodwill [1]\n"
            "📌 <b>GlobalFoundries (GFS, N/A):</b> TSMC deal — capacity [2]\n"
            "📌 <b>Foo (FOO, N/A):</b> no link — n/a [3]\n"
            "📌 <b>Bar (BAR, N/A):</b> made-up number — n/a [9]")
    out = bot.link_sources(text, HEADLINES)
    assert '<a href="https://example.com/nvda?a=1&amp;b=2">↗</a>' in out, out
    assert '<a href="https://example.com/gfs">↗</a>' in out, out
    # Missing link or out-of-range number: marker is dropped, not left dangling
    assert "[3]" not in out and "[9]" not in out, out
    assert out.count("<a ") == 2, out


def test_split_big_picture():
    llm = ("<b>🧭 Big Picture</b>\nChips lead as AI spending news piles up.\n\n"
           "<b>🔬 Semis + AI Headlines</b>\n📌 line\n\n<b>👀 One Thing To Watch</b>\nwatch")
    big, rest = bot.split_big_picture(llm)
    assert big == "<b>🧭 Big Picture</b>\nChips lead as AI spending news piles up.", big
    assert rest.startswith("<b>🔬 Semis + AI Headlines</b>"), rest

    # LLM skipped the section: nothing moves, nothing is lost
    big, rest = bot.split_big_picture("<b>🔬 Semis + AI Headlines</b>\n📌 line")
    assert big == "" and rest == "<b>🔬 Semis + AI Headlines</b>\n📌 line"


def test_br_tags_become_newlines():
    # gpt-oss-120b emits <br>; Telegram's HTML mode rejects it and the brief
    # degraded to plain text (no bold, no links) on 2026-11-09
    out = bot.clean_llm_html("<b>🧭 Big Picture</b><br>One.<br/>Two.<BR />Three.")
    assert out == "<b>🧭 Big Picture</b>\nOne.\nTwo.\nThree.", out


def test_market_sections_have_no_per_header_session_label():
    data = {"S&P 500": {"price": 6000.0, "change_pct": 0.5}}
    out = bot.build_market_sections(data, {"NVDA": 1.2, "AMD": -0.4})
    assert "vs prev close" not in out, out


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("All tests passed.")
