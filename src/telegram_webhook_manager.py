"""Telegram Webhook Manager for JAI 24/7 Cloudflare Worker.

Allows linking and unlinking Telegram webhook to a Cloudflare Worker URL with 1 click.
"""

import json
import os
from pathlib import Path
import sys
import urllib.parse
import urllib.request

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import config

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or getattr(config, "TELEGRAM_BOT_TOKEN", "")


def set_webhook(worker_url: str) -> dict:
    """Set Telegram Webhook URL."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook"
    params = {"url": worker_url}
    data = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def delete_webhook() -> dict:
    """Remove Telegram Webhook (restores local laptop polling mode)."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def get_webhook_info() -> dict:
    """Get current Webhook status."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getWebhookInfo"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd == "set" and len(sys.argv) > 2:
            res = set_webhook(sys.argv[2])
            print("Set Webhook Response:", res)
        elif cmd == "delete":
            res = delete_webhook()
            print("Delete Webhook Response:", res)
        elif cmd == "info":
            res = get_webhook_info()
            print("Webhook Info:", json.dumps(res, indent=2))
        else:
            print("Usage: python -m src.telegram_webhook_manager [set <url> | delete | info]")
    else:
        print("Webhook Info:", json.dumps(get_webhook_info(), indent=2))

