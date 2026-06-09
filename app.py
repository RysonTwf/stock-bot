import os

import requests
from flask import Flask, request, abort

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GITHUB_PAT         = os.environ["GITHUB_PAT"]
GITHUB_REPO        = "RysonTwf/stock-bot"
WORKFLOW_FILE      = "daily_brief.yml"


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


@app.route(f"/webhook/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
def webhook():
    if not request.is_json:
        abort(400)

    update  = request.get_json()
    message = update.get("message") or update.get("edited_message")
    if not message:
        return "ok"

    chat_id = str(message["chat"]["id"])
    text    = message.get("text", "").strip()

    if text.startswith("/brief"):
        ok = _trigger_github_actions(chat_id)
        if ok:
            _ack(chat_id, "⏳ Brief incoming — give it about a minute...")
        else:
            _ack(chat_id, "⚠️ Failed to trigger brief. Check GitHub Actions secrets.")

    return "ok"


@app.route("/", methods=["GET"])
def health():
    return "ok"


if __name__ == "__main__":
    app.run(debug=False)
