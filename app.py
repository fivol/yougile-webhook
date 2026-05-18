from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Set, Tuple

from fastapi import FastAPI, Request

ROOT = Path(__file__).parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
EVENT_LOG = LOG_DIR / "events.jsonl"
CLAUDE_LOG = LOG_DIR / "claude.log"


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_dotenv(ROOT / ".env")

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_WORKDIR = os.environ.get("CLAUDE_WORKDIR") or str(ROOT)
CLAUDE_EXTRA_ARGS = shlex.split(os.environ.get("CLAUDE_EXTRA_ARGS", "--dangerously-skip-permissions"))
CLAUDE_TRIGGER_EVENTS = {
    e.strip() for e in os.environ.get("CLAUDE_TRIGGER_EVENTS", "").split(",") if e.strip()
}
CLAUDE_TRIGGER_PATTERN = os.environ.get("CLAUDE_TRIGGER_PATTERN", "")
CLAUDE_MAX_CONCURRENT = int(os.environ.get("CLAUDE_MAX_CONCURRENT", "2"))

ALLOWED_USER_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("ALLOWED_USER_EMAILS", "").split(",")
    if e.strip()
}
SENDER_KEYS = [
    k.strip()
    for k in os.environ.get(
        "SENDER_KEYS", "fromUserId,createdBy,userId,authorId,senderId,from"
    ).split(",")
    if k.strip()
]

PROMPT_TEMPLATE_PATH = ROOT / os.environ.get("CLAUDE_PROMPT_TEMPLATE_FILE", "prompt_template.txt")
PROMPT_TEMPLATE = (
    PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    if PROMPT_TEMPLATE_PATH.exists()
    else "{event_json}"
)

_trigger_regex = re.compile(CLAUDE_TRIGGER_PATTERN) if CLAUDE_TRIGGER_PATTERN else None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("yougile-webhook")


def resolve_allowed_user_ids() -> Set[str]:
    if not ALLOWED_USER_EMAILS:
        return set()
    api_key = os.environ.get("YOUGILE_API_KEY")
    api_base = os.environ.get("YOUGILE_API_BASE", "https://yougile.com/api-v2").rstrip("/")
    if not api_key:
        log.warning("ALLOWED_USER_EMAILS set but YOUGILE_API_KEY missing — sender filter disabled")
        return set()
    ids: Set[str] = set()
    try:
        req = urllib.request.Request(
            f"{api_base}/users?limit=1000",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        for u in data.get("content", []):
            if (u.get("email") or "").lower() in ALLOWED_USER_EMAILS:
                ids.add(u["id"])
                log.info("allowed sender: %s -> %s", u.get("email"), u["id"])
        missing = ALLOWED_USER_EMAILS - {(u.get("email") or "").lower() for u in data.get("content", [])}
        if missing:
            log.warning("allowed emails not found in company users: %s", missing)
    except (urllib.error.URLError, json.JSONDecodeError):
        log.exception("failed to resolve allowed user IDs")
    return ids


ALLOWED_USER_IDS = resolve_allowed_user_ids()

app = FastAPI(title="YouGile webhook receiver")

_active_lock = threading.Lock()
_active_procs: Set[int] = set()


def extract_sender_id(payload) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    candidates = [payload]
    for nested_key in ("data", "payload", "message", "object"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            candidates.append(nested)
    for obj in candidates:
        for key in SENDER_KEYS:
            value = obj.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def event_should_trigger(payload) -> Tuple[bool, str]:
    if not isinstance(payload, dict):
        return False, "payload-not-dict"
    event = payload.get("event")
    if CLAUDE_TRIGGER_EVENTS and event not in CLAUDE_TRIGGER_EVENTS:
        return False, f"event-not-in-{sorted(CLAUDE_TRIGGER_EVENTS)}"
    if _trigger_regex:
        haystack = json.dumps(payload, ensure_ascii=False)
        if not _trigger_regex.search(haystack):
            return False, "pattern-no-match"
    if ALLOWED_USER_IDS:
        sender = extract_sender_id(payload)
        if sender not in ALLOWED_USER_IDS:
            return False, f"sender-not-allowed:{sender}"
    return True, "ok"


def spawn_claude(payload) -> None:
    with _active_lock:
        if len(_active_procs) >= CLAUDE_MAX_CONCURRENT:
            log.warning("claude concurrency cap %d reached, dropping event", CLAUDE_MAX_CONCURRENT)
            return

    event_json = json.dumps(payload, ensure_ascii=False, indent=2)
    prompt = PROMPT_TEMPLATE.replace("{event_json}", event_json)
    cmd = [CLAUDE_BIN, *CLAUDE_EXTRA_ARGS, "-p", prompt]

    ts = datetime.now(timezone.utc).isoformat()
    header = f"\n\n=== {ts} | spawn ===\ncwd: {CLAUDE_WORKDIR}\nbin: {CLAUDE_BIN}\nargs: {CLAUDE_EXTRA_ARGS}\n--- prompt ---\n{prompt}\n--- output ---\n"
    log_fp = CLAUDE_LOG.open("a", encoding="utf-8")
    log_fp.write(header)
    log_fp.flush()

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=CLAUDE_WORKDIR,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        log.exception("failed to spawn claude")
        log_fp.close()
        return

    pid = proc.pid
    with _active_lock:
        _active_procs.add(pid)
    log.info("claude spawned pid=%s", pid)

    def _reap():
        rc = proc.wait()
        with _active_lock:
            _active_procs.discard(pid)
        log_fp.write(f"\n--- exit rc={rc} ts={datetime.now(timezone.utc).isoformat()} ---\n")
        log_fp.close()
        log.info("claude pid=%s exited rc=%s", pid, rc)

    threading.Thread(target=_reap, daemon=True).start()


@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "claude_bin": CLAUDE_BIN,
        "claude_workdir": CLAUDE_WORKDIR,
        "trigger_events": sorted(CLAUDE_TRIGGER_EVENTS),
        "trigger_pattern": CLAUDE_TRIGGER_PATTERN,
        "allowed_user_emails": sorted(ALLOWED_USER_EMAILS),
        "allowed_user_ids": sorted(ALLOWED_USER_IDS),
        "active_claude_procs": len(_active_procs),
    }


@app.post("/yougile/webhook")
async def yougile_webhook(req: Request):
    raw = await req.body()
    try:
        payload = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        payload = {"_raw": raw.decode("utf-8", errors="replace")}

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "remote": req.client.host if req.client else None,
        "headers": dict(req.headers),
        "payload": payload,
    }
    with EVENT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    fire, reason = event_should_trigger(payload)
    if fire:
        spawn_claude(payload)

    log.info(
        "event=%s fired=%s reason=%s",
        isinstance(payload, dict) and payload.get("event"),
        fire,
        reason,
    )
    return {"ok": True, "claude_triggered": fire, "reason": reason}
