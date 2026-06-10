import os
import time
import threading

import requests
from flask import Flask, request, abort

app = Flask(__name__)

TELEGRAM_BOT_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
GITHUB_PAT            = os.environ["GITHUB_PAT"]
GITHUB_REPO           = "RysonTwf/stock-bot"
WORKFLOW_FILE         = "daily_brief.yml"
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

    if text.startswith("/start") or text.startswith("/help"):
        _ack(chat_id,
             "👋 I post a daily market brief every weekday automatically.\n"
             "Send /brief to get one on demand (limit 5/hour).")
        return "ok"

    if text.startswith("/brief"):
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
