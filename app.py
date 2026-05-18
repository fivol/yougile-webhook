from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import threading
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

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
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv(ROOT / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("yougile-webhook")


YOUGILE_API_KEY = os.environ.get("YOUGILE_API_KEY", "")
YOUGILE_API_BASE = os.environ.get("YOUGILE_API_BASE", "https://yougile.com/api-v2").rstrip("/")

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_DEFAULT_WORKDIR = os.environ.get("CLAUDE_WORKDIR") or str(ROOT)
CLAUDE_DEFAULT_EXTRA_ARGS = shlex.split(
    os.environ.get("CLAUDE_EXTRA_ARGS", "--dangerously-skip-permissions")
)
CLAUDE_MAX_CONCURRENT = int(os.environ.get("CLAUDE_MAX_CONCURRENT", "2"))

SENDER_KEYS = [
    k.strip()
    for k in os.environ.get(
        "SENDER_KEYS", "fromUserId,createdBy,userId,authorId,senderId,from"
    ).split(",")
    if k.strip()
]
COLUMN_KEYS = [
    k.strip()
    for k in os.environ.get(
        "COLUMN_KEYS", "columnId,toColumnId,newColumnId,destinationColumnId"
    ).split(",")
    if k.strip()
]

RULES_FILE = ROOT / os.environ.get("RULES_FILE", "rules.toml")


@dataclass
class Rule:
    name: str
    enabled: bool
    events: Set[str]
    pattern: Optional[re.Pattern]
    allowed_sender_emails: List[str]
    column_names: List[str]
    prompt_template: str
    workdir: str
    extra_args: List[str]
    # When True and column_names is set: only fire if the task actually transitioned
    # into the column (prevData.columnId != payload.columnId). Otherwise every
    # task-updated event while the task already sits in that column would re-fire.
    column_transition_only: bool = True
    allowed_sender_ids: Set[str] = field(default_factory=set)
    column_ids: Set[str] = field(default_factory=set)


def load_rules(path: Path) -> List[Rule]:
    if not path.exists():
        log.error(
            "rules file %s not found — copy rules.example.toml to rules.toml and edit",
            path,
        )
        return []
    with path.open("rb") as f:
        data = tomllib.load(f)
    rules: List[Rule] = []
    for raw in data.get("rules", []):
        name = raw.get("name", "<unnamed>")
        prompt_path = ROOT / raw.get("prompt_file", "")
        if not prompt_path.exists():
            log.warning("rule %s: prompt file %s missing — skipping", name, prompt_path)
            continue
        rules.append(
            Rule(
                name=name,
                enabled=bool(raw.get("enabled", True)),
                events=set(raw.get("events", [])),
                pattern=re.compile(raw["pattern"]) if raw.get("pattern") else None,
                allowed_sender_emails=list(raw.get("allowed_sender_emails", [])),
                column_names=list(raw.get("column_names", [])),
                prompt_template=prompt_path.read_text(encoding="utf-8"),
                workdir=raw.get("workdir") or CLAUDE_DEFAULT_WORKDIR,
                extra_args=(
                    shlex.split(raw["extra_args"])
                    if raw.get("extra_args") is not None
                    else list(CLAUDE_DEFAULT_EXTRA_ARGS)
                ),
                column_transition_only=bool(raw.get("column_transition_only", True)),
            )
        )
    return rules


def _api_get(path: str) -> Optional[dict]:
    if not YOUGILE_API_KEY:
        return None
    req = urllib.request.Request(
        f"{YOUGILE_API_BASE}{path}",
        headers={"Authorization": f"Bearer {YOUGILE_API_KEY}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError):
        log.exception("API GET failed: %s", path)
        return None


def resolve_rules(rules: List[Rule]) -> None:
    users = _api_get("/users?limit=1000") or {}
    columns = _api_get("/columns?limit=1000") or {}
    email_to_id: Dict[str, str] = {
        (u.get("email") or "").lower(): u["id"] for u in users.get("content", [])
    }
    name_to_ids: Dict[str, Set[str]] = {}
    for c in columns.get("content", []):
        key = (c.get("title") or "").lower()
        if key:
            name_to_ids.setdefault(key, set()).add(c["id"])

    for r in rules:
        for email in r.allowed_sender_emails:
            uid = email_to_id.get(email.lower())
            if uid:
                r.allowed_sender_ids.add(uid)
            else:
                log.warning("rule %s: email %s not found in company users", r.name, email)
        for cname in r.column_names:
            ids = name_to_ids.get(cname.lower())
            if ids:
                r.column_ids.update(ids)
            else:
                log.warning("rule %s: column '%s' not found", r.name, cname)
        log.info(
            "rule %s: enabled=%s events=%s pattern=%s senders=%d columns=%d workdir=%s",
            r.name,
            r.enabled,
            sorted(r.events),
            r.pattern.pattern if r.pattern else None,
            len(r.allowed_sender_ids),
            len(r.column_ids),
            r.workdir,
        )


RULES: List[Rule] = load_rules(RULES_FILE)
resolve_rules(RULES)


def _deep_get(payload, keys: List[str]) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    candidates = [payload]
    for nested_key in ("data", "payload", "message", "object", "task"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            candidates.append(nested)
    for obj in candidates:
        for key in keys:
            value = obj.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def rule_matches(rule: Rule, payload) -> Tuple[bool, str]:
    if not rule.enabled:
        return False, "disabled"
    if not isinstance(payload, dict):
        return False, "payload-not-dict"
    event = payload.get("event")
    if rule.events and event not in rule.events:
        return False, "event-mismatch"
    if rule.pattern:
        haystack = json.dumps(payload, ensure_ascii=False)
        if not rule.pattern.search(haystack):
            return False, "pattern-no-match"
    if rule.allowed_sender_emails and not rule.allowed_sender_ids:
        return False, "no-allowed-senders-resolved"
    if rule.allowed_sender_ids:
        sender = _deep_get(payload, SENDER_KEYS)
        if sender not in rule.allowed_sender_ids:
            return False, f"sender-not-allowed:{sender}"
    if rule.column_names and not rule.column_ids:
        return False, "no-columns-resolved"
    if rule.column_ids:
        col = _deep_get(payload, COLUMN_KEYS)
        if col not in rule.column_ids:
            return False, f"column-not-allowed:{col}"
        if rule.column_transition_only:
            prev_data = payload.get("prevData") if isinstance(payload, dict) else None
            prev_col: Optional[str] = None
            if isinstance(prev_data, dict):
                for key in COLUMN_KEYS:
                    v = prev_data.get(key)
                    if isinstance(v, str) and v:
                        prev_col = v
                        break
            if prev_col == col:
                return False, "no-column-transition"
    return True, "ok"


def find_matching_rule(payload) -> Tuple[Optional[Rule], List[Tuple[str, str]]]:
    """First-match-wins. Returns (rule, [(rule_name, reason), ...]) for debugging."""
    reasons: List[Tuple[str, str]] = []
    for r in RULES:
        ok, why = rule_matches(r, payload)
        reasons.append((r.name, why))
        if ok:
            return r, reasons
    return None, reasons


app = FastAPI(title="YouGile webhook receiver")

_active_lock = threading.Lock()
_active_procs: Set[int] = set()


def spawn_claude(rule: Rule, payload) -> bool:
    with _active_lock:
        if len(_active_procs) >= CLAUDE_MAX_CONCURRENT:
            log.warning(
                "rule %s: max-concurrent %d reached, dropping",
                rule.name,
                CLAUDE_MAX_CONCURRENT,
            )
            return False

    event_json = json.dumps(payload, ensure_ascii=False, indent=2)
    prompt = rule.prompt_template.replace("{event_json}", event_json)
    cmd = [CLAUDE_BIN, *rule.extra_args, "-p", prompt]

    ts = datetime.now(timezone.utc).isoformat()
    header = (
        f"\n\n=== {ts} | rule={rule.name} | spawn ===\n"
        f"cwd: {rule.workdir}\nbin: {CLAUDE_BIN}\nargs: {rule.extra_args}\n"
        f"--- prompt ---\n{prompt}\n--- output ---\n"
    )
    log_fp = CLAUDE_LOG.open("a", encoding="utf-8")
    log_fp.write(header)
    log_fp.flush()

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=rule.workdir,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        log.exception("rule %s: failed to spawn claude", rule.name)
        log_fp.close()
        return False

    pid = proc.pid
    with _active_lock:
        _active_procs.add(pid)
    log.info("rule %s: claude spawned pid=%s", rule.name, pid)

    def _reap():
        rc = proc.wait()
        with _active_lock:
            _active_procs.discard(pid)
        log_fp.write(
            f"\n--- exit rc={rc} ts={datetime.now(timezone.utc).isoformat()} ---\n"
        )
        log_fp.close()
        log.info("rule %s: claude pid=%s rc=%s", rule.name, pid, rc)

    threading.Thread(target=_reap, daemon=True).start()
    return True


@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "claude_bin": CLAUDE_BIN,
        "default_workdir": CLAUDE_DEFAULT_WORKDIR,
        "active_claude_procs": len(_active_procs),
        "rules": [
            {
                "name": r.name,
                "enabled": r.enabled,
                "events": sorted(r.events),
                "pattern": r.pattern.pattern if r.pattern else None,
                "allowed_sender_ids": sorted(r.allowed_sender_ids),
                "column_ids": sorted(r.column_ids),
                "workdir": r.workdir,
            }
            for r in RULES
        ],
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

    rule, reasons = find_matching_rule(payload)
    fired_rule: Optional[str] = None
    if rule and spawn_claude(rule, payload):
        fired_rule = rule.name

    event = isinstance(payload, dict) and payload.get("event")
    log.info("event=%s fired=%s reasons=%s", event, fired_rule, reasons)
    return {"ok": True, "fired_rule": fired_rule, "reasons": reasons}
