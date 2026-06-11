# Daily Stock Market Brief Bot

A Telegram bot that sends a market briefing every weekday at 7:00 PM SGT (11:00 UTC), covering S&P 500, Nasdaq, Dow, plus top semiconductor and AI/Big Tech headlines summarised by Groq's LLaMA 3.3 70B.

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

After the US close, watchlist lines split the move into the regular-session change and the after-hours change, e.g. `▲ NVDA: 201.50 (day +1.20%, AH +0.45%)`. Quotes are fetched via yfinance with a direct Yahoo chart-API fallback, and previous closes are cached for the day to cut request volume.

The watchlist is shared by everyone in the group (single-group bot). It is stored as `watchlist.json` in this repo: the webhook commits changes via the GitHub API, and the daily brief picks the file up automatically on its next run, showing a **⭐ Watchlist** section with live prices.

---

## Files

| File | Purpose |
|------|---------|
| `bot.py` | Core logic — market data, headlines, Groq prompt, Telegram send |
| `app.py` | Flask webhook server — handles `/brief` and watchlist commands from Telegram |
| `watchlist.py` | Shared watchlist module — live quotes (pre/post market), validation, formatting |
| `watchlist.json` | The shared watchlist itself (committed by the webhook via GitHub API) |
| `requirements.txt` | Python dependencies |
| `.github/workflows/daily_brief.yml` | GitHub Actions scheduler (Mon–Fri, 11:00 UTC) |
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
📊 Market Pulse
S&P 500: $5,432.10 ▲ +0.84%
Nasdaq:  $17,891.50 ▲ +1.12%
Dow:     $39,215.30 ▼ -0.21%
Green open led by tech; Dow lagging on industrials drag.

⭐ Watchlist (pre-market, vs prev close)
▲ NVDA: 200.21 (+2.10%)
▼ TSLA: 384.77 (-0.83%)

🔬 Semis + AI Headlines
1. Nvidia's Blackwell GPU demand outstrips supply into Q3
2. TSMC raises 2025 revenue forecast on AI chip orders
...

👀 One Thing To Watch
Nvidia supply constraints are the dominant story today...
```

---

## Customisation

- **Timing**: Edit the `cron` field in `daily_brief.yml`. Use [crontab.guru](https://crontab.guru) to build expressions.
- **Extra tickers**: Add symbols to the `INDICES` dict in `bot.py` (any valid yfinance ticker).
- **RSS feeds**: Add more URLs to the `RSS_FEEDS` list.
- **Tone / format**: Edit the prompt in `build_prompt()` inside `bot.py`.
