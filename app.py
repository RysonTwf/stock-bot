import base64
import html
import json
import os
import time
import threading

import requests
from flask import Flask, request, abort

import watchlist as wl

app = Flask(__name__)

TELEGRAM_BOT_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
GITHUB_PAT            = os.environ["GITHUB_PAT"]
GITHUB_REPO           = "RysonTwf/stock-bot"
WORKFLOW_FILE         = "daily_brief.yml"
WATCHLIST_API_URL     = f"https://api.github.com/repos/{GITHUB_REPO}/contents/watchlist.json"
GH_HEADERS            = {
    "Authorization": f"Bearer {GITHUB_PAT}",
    "Accept":        "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
MAX_REQUESTS_PER_HOUR = 5

_request_log: dict[str, list[float]] = {}  # chat_id -> list of timestamps
_seen_updates: set[int] = set()            # deduplicate Telegram retries


def _ack(chat_id: str, text: str) -> None:
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        timeout=10,
    )


def _trigger_github_actions(chat_id: str) -> bool:
    resp = requests.post(
        f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches",
        headers={
            "Authorization": f"Bearer {GITHUB_PAT}",
            "Accept":        "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"ref": "main", "inputs": {"chat_id": chat_id}},
        timeout=15,
    )
    return resp.status_code == 204


def _dispatch_brief(chat_id: str, slot: int) -> None:
    ok = _trigger_github_actions(chat_id)
    if ok:
        _ack(chat_id, f"⏳ Brief incoming — give it about a minute... ({slot}/5 this hour)")
    else:
        _ack(chat_id, "⚠️ Failed to trigger brief. Check GitHub Actions secrets.")


# ---------------------------------------------------------------------------
# Shared watchlist — stored as watchlist.json in the GitHub repo so both this
# webhook and the Actions-run brief see the same list
# ---------------------------------------------------------------------------
def _gh_load_watchlist() -> tuple[list[str], str | None]:
    """Return (tickers, file_sha). sha is needed to commit an update."""
    resp = requests.get(WATCHLIST_API_URL, headers=GH_HEADERS, timeout=15)
    if resp.status_code == 404:
        return [], None
    resp.raise_for_status()
    data = resp.json()
    tickers = json.loads(base64.b64decode(data["content"]).decode("utf-8"))
    return [t for t in tickers if isinstance(t, str)], data["sha"]


def _gh_save_watchlist(tickers: list[str], sha: str | None, action: str) -> bool:
    body = {
        "message": f"watchlist: {action}",
        "content": base64.b64encode(
            (json.dumps(tickers, indent=2) + "\n").encode("utf-8")
        ).decode("ascii"),
        "branch": "main",
    }
    if sha:
        body["sha"] = sha
    resp = requests.put(WATCHLIST_API_URL, headers=GH_HEADERS, json=body, timeout=15)
    return resp.status_code in (200, 201)


def _handle_watch(chat_id: str, args: list[str]) -> None:
    if not args:
        _ack(chat_id, "Usage: /watch TICKER [TICKER ...] — e.g. /watch NVDA TSLA")
        return
    try:
        current, sha = _gh_load_watchlist()
    except Exception:
        _ack(chat_id, "⚠️ Couldn't load the watchlist from GitHub — try again later.")
        return

    added, already, invalid = [], [], []
    for raw in args[:10]:
        t = raw.upper().lstrip("$")
        if t in current or t in added:
            already.append(t)
        elif len(current) + len(added) >= wl.MAX_TICKERS:
            _ack(chat_id, f"🚫 Watchlist is full ({wl.MAX_TICKERS} max) — /unwatch something first.")
            return
        elif wl.is_valid_ticker(t):
            added.append(t)
        else:
            invalid.append(t)

    if added and not _gh_save_watchlist(current + added, sha, f"add {', '.join(added)}"):
        _ack(chat_id, "⚠️ Failed to save the watchlist — try again later.")
        return

    parts = []
    if added:
        parts.append(f"✅ Added: {', '.join(added)}")
    if already:
        parts.append(f"Already watching: {', '.join(already)}")
    if invalid:
        parts.append(f"⚠️ Not found on Yahoo Finance: {html.escape(', '.join(invalid))}")
    _ack(chat_id, "\n".join(parts) or "Nothing to add.")


def _handle_unwatch(chat_id: str, args: list[str]) -> None:
    if not args:
        _ack(chat_id, "Usage: /unwatch TICKER [TICKER ...]")
        return
    try:
        current, sha = _gh_load_watchlist()
    except Exception:
        _ack(chat_id, "⚠️ Couldn't load the watchlist from GitHub — try again later.")
        return

    wanted  = {raw.upper().lstrip("$") for raw in args}
    removed = [t for t in current if t in wanted]
    kept    = [t for t in current if t not in wanted]
    missing = sorted(wanted - set(removed))

    if removed and not _gh_save_watchlist(kept, sha, f"remove {', '.join(removed)}"):
        _ack(chat_id, "⚠️ Failed to save the watchlist — try again later.")
        return

    parts = []
    if removed:
        parts.append(f"🗑 Removed: {', '.join(removed)}")
    if missing:
        parts.append(f"Wasn't on the list: {html.escape(', '.join(missing))}")
    _ack(chat_id, "\n".join(parts))


def _handle_watchlist(chat_id: str) -> None:
    try:
        tickers, _ = _gh_load_watchlist()
    except Exception:
        _ack(chat_id, "⚠️ Couldn't load the watchlist from GitHub — try again later.")
        return
    quotes = wl.get_live_quotes(tickers)
    _ack(chat_id, wl.format_watchlist(tickers, quotes))


@app.route(f"/webhook/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
def webhook():
    if not request.is_json:
        abort(400)

    update = request.get_json()

    # Deduplicate: Telegram resends the same update_id if we don't respond fast enough
    update_id = update.get("update_id")
    if update_id in _seen_updates:
        return "ok"
    _seen_updates.add(update_id)
    # Keep the set bounded — retain only the 250 highest IDs (Telegram IDs are monotonically increasing)
    if len(_seen_updates) > 500:
        keep = sorted(_seen_updates)[-250:]
        _seen_updates.clear()
        _seen_updates.update(keep)

    message = update.get("message") or update.get("edited_message")
    if not message:
        return "ok"

    chat_id = str(message["chat"]["id"])
    text    = message.get("text", "").strip()

    # "/watch@MyBot NVDA" in groups -> command "/watch", args ["NVDA"]
    parts   = text.split()
    command = parts[0].split("@")[0].lower() if parts else ""
    args    = parts[1:]

    if command in ("/start", "/help"):
        _ack(chat_id,
             "👋 I post a daily market brief every weekday automatically.\n"
             "/brief — get a brief now (limit 5/hour)\n"
             "/watch NVDA TSLA — add stocks to the shared watchlist\n"
             "/unwatch NVDA — remove stocks\n"
             "/watchlist — live prices (works pre/post market too)")
        return "ok"

    # Watchlist commands hit GitHub + Yahoo, so run off the webhook thread
    if command == "/watch":
        threading.Thread(target=_handle_watch, args=(chat_id, args), daemon=True).start()
        return "ok"

    if command == "/unwatch":
        threading.Thread(target=_handle_unwatch, args=(chat_id, args), daemon=True).start()
        return "ok"

    if command == "/watchlist":
        threading.Thread(target=_handle_watchlist, args=(chat_id,), daemon=True).start()
        return "ok"

    if command == "/brief":
        now     = time.time()
        window  = now - 3600
        history = [t for t in _request_log.get(chat_id, []) if t > window]

        if len(history) >= MAX_REQUESTS_PER_HOUR:
            oldest       = min(history)
            reset_in_min = int((oldest + 3600 - now) / 60) + 1
            _ack(chat_id, f"🚫 Limit reached (5/hr) — resets in {reset_in_min} min.")
            return "ok"

        # Optimistically reserve a slot, then dispatch in background so Telegram
        # gets a fast response and won't retry the same update
        slot = len(history) + 1
        _request_log[chat_id] = history + [now]
        threading.Thread(target=_dispatch_brief, args=(chat_id, slot), daemon=True).start()

    return "ok"


@app.route("/", methods=["GET"])
def health():
    return "ok"


if __name__ == "__main__":
    app.run(debug=False)
