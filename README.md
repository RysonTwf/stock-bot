# Daily Stock Market Brief Bot

A Telegram bot that sends a market briefing every weekday at the US market open (14:30 UTC, 10:30 PM SGT; GitHub can start scheduled runs a few minutes late), covering S&P 500, Nasdaq, Dow, plus a two-line Big Picture and the top semiconductor and AI/Big Tech headlines picked and summarised by Groq's `openai/gpt-oss-20b`.

Also supports on-demand Telegram commands via a Flask webhook hosted on PythonAnywhere, including a shared watchlist with live pre/post-market prices.

---

## Telegram Commands

| Command | What it does |
|---------|--------------|
| `/brief` | Trigger a full brief now (limit 5/hour) |
| `/watch NVDA TSLA` | Add stocks to the shared watchlist (validated against Yahoo Finance, max 30) |
| `/unwatch NVDA` | Remove stocks from the watchlist |
| `/watchlist` | Live prices for the watchlist — works during pre-market and after-hours too |
| `/help` | List all commands |

Watchlist lines always show the current price (regular, pre-market, or after-hours) against the previous session close, e.g. `▲ NVDA: 201.50 (+1.20%)`. The Movers section and the `(TICKER, ±X%)` headline annotations in the brief use the same live quote source, so all numbers agree. Quotes come from Yahoo's chart API directly (one call per ticker, correct prev closes) with the yfinance library as fallback. During pre-market, Yahoo's `chartPreviousClose` is two sessions old (the chart's "current day" is still the prior session), so the bot uses `regularMarketPrice` — the last completed close — as the baseline in that window; `tests/test_prev_close.py` covers this.

The watchlist is shared by everyone in the group (single-group bot). It is stored as `watchlist.json` in this repo: the webhook commits changes via the GitHub API, and the daily brief picks the file up automatically on its next run, showing a **⭐ Watchlist** section with live prices.

---

## Files

| File | Purpose |
|------|---------|
| `bot.py` | Core logic — market data, headlines, Groq prompt, Telegram send |
| `app.py` | Flask webhook server — handles `/brief` and watchlist commands from Telegram |
| `watchlist.py` | Shared watchlist module — live quotes (pre/post market), validation, formatting |
| `watchlist.json` | The shared watchlist itself (committed by the webhook via GitHub API) |
| `tests/test_brief_format.py` | Standalone test (no pytest) for the headline filter, source links and Big Picture placement |
| `tests/test_prev_close.py` | Standalone test (no pytest) for prev-close selection; run it directly for a live AMD/NVDA sanity check |
| `requirements.txt` | Python dependencies |
| `.github/workflows/daily_brief.yml` | GitHub Actions scheduler (Mon–Fri, 14:30 UTC) |
| `.github/workflows/keepalive.yml` | Monthly empty commit so GitHub doesn't disable the schedule after 60 days of inactivity |
| `CLAUDE.md` | Architecture notes for AI-assisted development |

---

## Setup

### 1. Get a Telegram Bot Token

1. Open Telegram and search for **@BotFather**.
2. Send `/newbot` and follow the prompts (pick a name and username).
3. BotFather will give you a token like `123456789:ABCdef...` — save it.

### 2. Get Your Telegram Chat ID

**Option A — personal chat with the bot:**
1. Start a conversation with your new bot (search it by username and press Start).
2. Send any message to it.
3. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser.
4. Find `"chat":{"id": 123456789}` in the JSON — that number is your Chat ID.

**Option B — a group or channel:**
1. Add the bot to the group/channel and send a message.
2. Use the same `getUpdates` URL above; the chat ID for groups starts with `-`.

### 3. Get a Groq API Key

1. Go to [console.groq.com](https://console.groq.com) and sign up (free).
2. Navigate to **API Keys** → **Create API Key**.
3. Copy the key.

### 4. Add Secrets to GitHub

In your GitHub repo go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret name | Value |
|-------------|-------|
| `TELEGRAM_BOT_TOKEN` | Your BotFather token |
| `TELEGRAM_CHAT_ID` | Your chat/group ID |
| `GROQ_API_KEY` | Your Groq API key |

### 5. Push and Enable Actions

```bash
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

Go to the **Actions** tab in GitHub and confirm workflows are enabled. The brief will fire automatically at 00:00 UTC daily.

To test immediately, click **Actions → Daily Stock Brief → Run workflow**.

---

## `/brief` On-Demand Command — PythonAnywhere Setup

The `app.py` Flask app listens for Telegram webhook updates. When you send `/brief` to your bot, it runs the full briefing immediately.

PythonAnywhere free tier gives you a persistent HTTPS URL, which is required for Telegram webhooks.

### 1. Create a PythonAnywhere account

Sign up free at [pythonanywhere.com](https://www.pythonanywhere.com). Your app will be hosted at:
```
https://YOUR_USERNAME.pythonanywhere.com
```

### 2. Upload the project files

In the PythonAnywhere dashboard open a **Bash console** and run:

```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git ~/stocks
cd ~/stocks
pip install --user -r requirements.txt
```

### 3. Set environment variables

In the PythonAnywhere Bash console, add your secrets to `~/.bashrc` (used by the web app):

```bash
cat >> ~/.bashrc << 'EOF'
export TELEGRAM_BOT_TOKEN="your_token_here"
export TELEGRAM_CHAT_ID="your_chat_id_here"
export GROQ_API_KEY="your_groq_key_here"
export GITHUB_PAT="your_github_pat_here"
EOF
source ~/.bashrc
```

`GITHUB_PAT` needs **Actions: read/write** (to dispatch `/brief`) and **Contents: read/write** (to commit watchlist changes) on this repo.

### 4. Create the Web App

1. Go to **Web** tab → **Add a new web app**.
2. Choose **Manual configuration** → **Python 3.10**.
3. Set **Source code** to `/home/YOUR_USERNAME/stocks`.
4. Set **WSGI configuration file** — click the link to edit it and replace the entire contents with:

```python
import sys
import os

sys.path.insert(0, '/home/YOUR_USERNAME/stocks')

os.environ['TELEGRAM_BOT_TOKEN'] = 'your_token_here'
os.environ['TELEGRAM_CHAT_ID']   = 'your_chat_id_here'
os.environ['GROQ_API_KEY']       = 'your_groq_key_here'
os.environ['GITHUB_PAT']         = 'your_github_pat_here'

from app import app as application
```

5. Click **Reload** to start the app.

### 5. Register the Telegram Webhook

Run this once in any browser or terminal (replace placeholders):

```
https://api.telegram.org/bot<YOUR_TOKEN>/setWebhook?url=https://YOUR_USERNAME.pythonanywhere.com/webhook/<YOUR_TOKEN>
```

You should get: `{"ok":true,"result":true,"description":"Webhook was set"}`

To verify it's registered:
```
https://api.telegram.org/bot<YOUR_TOKEN>/getWebhookInfo
```

### 6. Test it

Send `/brief` to your bot in Telegram. You'll get an instant "⏳ Fetching..." acknowledgement followed by the full brief within ~15 seconds.

### Deploying code changes

After pushing changes to `app.py` or `watchlist.py`, update the PythonAnywhere clone and reload:

```bash
cd ~/stocks && git pull
```

Then click **Reload** on the Web tab. (Watchlist *data* changes need nothing — the webhook reads/writes `watchlist.json` via the GitHub API, and the daily brief checks out fresh `main` every run.)

---

## Running Locally

```bash
pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
export GROQ_API_KEY="..."

python bot.py
```

On Windows (PowerShell):
```powershell
$env:TELEGRAM_BOT_TOKEN = "..."
$env:TELEGRAM_CHAT_ID   = "..."
$env:GROQ_API_KEY       = "..."
python bot.py
```

---

## What the Brief Looks Like

```
US market: regular hours · all moves vs prev close

🧭 Big Picture
Chip stocks are split as Nvidia's governance scare weighs on the leader while
memory and equipment names rally. AI spending headlines keep tech bid overall.

📊 Market Pulse
▲ S&P 500: 7,376.86 (+0.31%)
▲ Nasdaq: 24,579.42 (+1.09%)
▼ Dow: 51,285.73 (-0.46%)
SMH ▼ -0.12% | XLK ▲ +0.08%
VIX: 23.5 (-8.2%) — moderate

📈 Movers
▲ AMD +3.9% | ASML +3.3% | AMZN +2.9%
▼ NVDA -5.1% | SMCI -2.4% | NXPI -0.7%

⭐ Watchlist
▼ NVDA: 177.05 (-5.14%)
▲ AMD: 466.63 (+3.92%)

🔬 Semis + AI Headlines
📌 Nvidia (NVDA, -5.1%): Board opens probe into CEO spying case — governance risk hits the AI leader ↗
...

👀 One Thing To Watch
...
```

The session (pre-market, regular hours, after-hours or market closed) is stated once at the top, and every section uses the same live quote source, so the numbers are directly comparable. Each headline ends with a ↗ link to its article. Before the AI sees the headlines, the bot drops listicles, buy/sell advice, teaser titles and Motley Fool syndication (`is_low_value_headline()` in `bot.py`); the AI then picks the 5 most important. The AI never supplies numbers: price moves and links are filled in by the bot.

---

## Customisation

- **Timing**: Edit the `cron` field in `daily_brief.yml`. Use [crontab.guru](https://crontab.guru) to build expressions.
- **Extra tickers**: Add symbols to the `INDICES` dict in `bot.py` (any valid yfinance ticker).
- **RSS feeds**: Add more URLs to the `RSS_FEEDS` list.
- **Tone / format**: Edit the prompt in `build_prompt()` inside `bot.py`.
