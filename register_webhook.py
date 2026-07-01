"""Register / list / delete YouGile webhook subscriptions.

All settings come from .env. Usage:

    python register_webhook.py list
    python register_webhook.py create
    python register_webhook.py delete <id>
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv(ROOT / ".env")

API_KEY = os.environ["YOUGILE_API_KEY"]
API_BASE = os.environ.get("YOUGILE_API_BASE", "https://yougile.com/api-v2").rstrip("/")
WEBHOOK_URL = os.environ["YOUGILE_WEBHOOK_URL"]
EVENT = os.environ.get("YOUGILE_WEBHOOK_EVENT", "chat_message-created")
CHAT_FILTER = os.environ.get("YOUGILE_WEBHOOK_CHAT_FILTER", "")


def _request(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def cmd_list():
    status, data = _request("GET", "/webhooks")
    print(json.dumps({"status": status, "data": data}, ensure_ascii=False, indent=2))


def _our_subscriptions():
    _, data = _request("GET", "/webhooks")
    rows = data
    if isinstance(rows, dict):
        rows = rows.get("content") or rows.get("data") or []
    return [s for s in (rows or []) if isinstance(s, dict) and s.get("url") == WEBHOOK_URL]


def cmd_create():
    # Refuse to create a duplicate: the service keeps exactly one live
    # subscription per URL, and a second live row makes YouGile deliver every
    # event twice. If a live one already exists, do nothing.
    live = [s for s in _our_subscriptions() if not s.get("disabled")]
    if live:
        print(json.dumps({
            "skipped": "a live subscription for this URL already exists — not creating a duplicate",
            "existing": [s.get("id") for s in live],
        }, ensure_ascii=False, indent=2))
        return
    body: dict = {"url": WEBHOOK_URL, "event": EVENT}
    if CHAT_FILTER:
        body["filters"] = [{"name": "chat_message", "value": CHAT_FILTER}]
    status, data = _request("POST", "/webhooks", body)
    print(json.dumps({"status": status, "data": data, "sent": body}, ensure_ascii=False, indent=2))


def cmd_delete(hook_id: str):
    status, data = _request("PUT", f"/webhooks/{hook_id}", {"deleted": True})
    print(json.dumps({"status": status, "data": data}, ensure_ascii=False, indent=2))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    op = sys.argv[1]
    if op == "list":
        cmd_list()
    elif op == "create":
        cmd_create()
    elif op == "delete" and len(sys.argv) >= 3:
        cmd_delete(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
