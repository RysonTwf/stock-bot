import os
import json
import threading

from flask import Flask, request, abort
import requests

from bot import get_market_data, get_headlines, build_prompt, call_groq, send_telegram

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]


def _send_brief_async(chat_id: str) -> None:
    try:
        market_data = get_market_data()
        headlines   = get_headlines()
        prompt      = build_prompt(market_data, headlines)
        brief       = call_groq(prompt)
        send_telegram(brief, chat_id=chat_id)
    except Exception as e:
        error_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(error_url, json={
            "chat_id":    chat_id,
            "text":       f"⚠️ Brief failed: {e}",
            "parse_mode": "HTML",
        }, timeout=10)


@app.route(f"/webhook/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
def webhook():
    if not request.is_json:
        abort(400)

    update = request.get_json()
    message = update.get("message") or update.get("edited_message")
    if not message:
        return "ok"

    chat_id = str(message["chat"]["id"])
    text    = message.get("text", "").strip()

    if text.startswith("/brief"):
        # Acknowledge immediately so Telegram doesn't retry
        ack_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(ack_url, json={
            "chat_id":    chat_id,
            "text":       "⏳ Fetching your brief, one moment...",
            "parse_mode": "HTML",
        }, timeout=10)
        # Run the heavy work in a background thread
        threading.Thread(target=_send_brief_async, args=(chat_id,), daemon=True).start()

    return "ok"


@app.route("/", methods=["GET"])
def health():
    return "ok"


if __name__ == "__main__":
    app.run(debug=False)
