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
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import FastAPI, Request

ROOT = Path(__file__).parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
STATE_DIR = ROOT / "state"
STATE_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / "sessions.json"
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
CHAT_ID_KEYS = [
    k.strip()
    for k in os.environ.get("CHAT_ID_KEYS", "chatId,id").split(",")
    if k.strip()
]

CHAT_HISTORY_FIRST_TIME_LIMIT = int(os.environ.get("CHAT_HISTORY_FIRST_TIME_LIMIT", "20"))
CHAT_HISTORY_DELTA_LIMIT = int(os.environ.get("CHAT_HISTORY_DELTA_LIMIT", "50"))

RULES_FILE = ROOT / os.environ.get("RULES_FILE", "rules.toml")

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


# ---------------------------------------------------------------- rule model

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
    column_transition_only: bool = True
    # Continue the same claude session across re-mentions on the same chat.
    session_per_chat: bool = True
    allowed_sender_ids: Set[str] = field(default_factory=set)
    column_ids: Set[str] = field(default_factory=set)


def load_rules(path: Path) -> List[Rule]:
    if not path.exists():
        log.error("rules file %s not found — copy rules.example.toml to rules.toml and edit", path)
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
                session_per_chat=bool(raw.get("session_per_chat", True)),
            )
        )
    return rules


# ---------------------------------------------------------------- API helpers

def _api_get(path: str) -> Optional[dict]:
    if not YOUGILE_API_KEY:
        return None
    req = urllib.request.Request(
        f"{YOUGILE_API_BASE}{path}",
        headers={"Authorization": f"Bearer {YOUGILE_API_KEY}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError):
        log.exception("API GET failed: %s", path)
        return None


USERS_BY_ID: Dict[str, Dict[str, Any]] = {}
USERS_BY_EMAIL: Dict[str, str] = {}
COLUMNS_BY_NAME: Dict[str, Set[str]] = {}


def refresh_directories() -> None:
    USERS_BY_ID.clear()
    USERS_BY_EMAIL.clear()
    COLUMNS_BY_NAME.clear()
    users = _api_get("/users?limit=1000") or {}
    for u in users.get("content", []):
        uid = u["id"]
        USERS_BY_ID[uid] = u
        em = (u.get("email") or "").lower()
        if em:
            USERS_BY_EMAIL[em] = uid
    columns = _api_get("/columns?limit=1000") or {}
    for c in columns.get("content", []):
        title = (c.get("title") or "").lower()
        if title:
            COLUMNS_BY_NAME.setdefault(title, set()).add(c["id"])


def resolve_rules(rules: List[Rule]) -> None:
    for r in rules:
        for email in r.allowed_sender_emails:
            uid = USERS_BY_EMAIL.get(email.lower())
            if uid:
                r.allowed_sender_ids.add(uid)
            else:
                log.warning("rule %s: email %s not found in company users", r.name, email)
        for cname in r.column_names:
            ids = COLUMNS_BY_NAME.get(cname.lower())
            if ids:
                r.column_ids.update(ids)
            else:
                log.warning("rule %s: column '%s' not found", r.name, cname)
        log.info(
            "rule %s: enabled=%s events=%s pattern=%s senders=%d columns=%d workdir=%s session_per_chat=%s",
            r.name, r.enabled, sorted(r.events),
            r.pattern.pattern if r.pattern else None,
            len(r.allowed_sender_ids), len(r.column_ids), r.workdir, r.session_per_chat,
        )


refresh_directories()
RULES: List[Rule] = load_rules(RULES_FILE)
resolve_rules(RULES)


# ---------------------------------------------------------------- payload extraction

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


def extract_chat_id(payload) -> Optional[str]:
    cid = _deep_get(payload, CHAT_ID_KEYS)
    if cid and _UUID_RE.match(cid):
        return cid
    return None


# ---------------------------------------------------------------- rule matching

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


# ---------------------------------------------------------------- state

_state_lock = threading.Lock()


def load_state() -> Dict[str, Dict[str, Any]]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.exception("state file corrupted, starting fresh")
        return {}


def save_state(state: Dict[str, Dict[str, Any]]) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def update_chat_state(chat_id: str, **fields: Any) -> Dict[str, Any]:
    with _state_lock:
        state = load_state()
        entry = state.setdefault(chat_id, {})
        entry.update(fields)
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return dict(entry)


def get_chat_state(chat_id: str) -> Dict[str, Any]:
    with _state_lock:
        return load_state().get(chat_id, {})


# ---------------------------------------------------------------- chat history

def fetch_chat_messages(
    chat_id: str, since: Optional[int] = None, limit: int = 20
) -> List[dict]:
    """Fetch up to `limit` messages, paginating if needed. Returns the LATEST
    `limit` messages (oldest first). `since` is exclusive — only messages with
    id strictly greater than `since` are returned.
    """
    PAGE = 1000  # YouGile API max
    page_limit = min(PAGE, max(1, limit))
    collected: List[dict] = []
    offset = 0
    while True:
        params = {
            "limit": str(page_limit),
            "offset": str(offset),
            "includeSystem": "false",
        }
        if since is not None:
            # Some YouGile deployments treat `since` as inclusive; shift by 1
            # to make it strictly "after" last_seen.
            params["since"] = str(int(since) + 1)
        qs = urllib.parse.urlencode(params)
        data = _api_get(f"/chats/{chat_id}/messages?{qs}") or {}
        items = data.get("content", []) or []
        if not items:
            break
        collected.extend(items)
        paging = data.get("paging") or {}
        if not paging.get("next"):
            break
        offset += len(items)
        if offset > 100_000:  # hard safety cap
            break
    collected.sort(key=lambda m: m.get("id") or 0)
    if limit and len(collected) > limit:
        collected = collected[-limit:]
    return collected


def _format_sender(user_id: Optional[str]) -> str:
    if not user_id:
        return "unknown"
    u = USERS_BY_ID.get(user_id)
    if not u:
        return f"user:{user_id[:8]}"
    return u.get("email") or u.get("realName") or f"user:{user_id[:8]}"


def _claude_session_file(workdir: str, session_id: str) -> Path:
    """Path where claude code stores a session's transcript.

    Claude encodes the workdir by replacing every "/" with "-" and uses the
    resulting string as the directory name under ~/.claude/projects/.
    """
    encoded = workdir.replace("/", "-")
    return Path.home() / ".claude" / "projects" / encoded / f"{session_id}.jsonl"


def claude_session_exists(workdir: str, session_id: str) -> bool:
    return _claude_session_file(workdir, session_id).exists()


def render_chat_history(messages: List[dict]) -> str:
    if not messages:
        return "(no prior messages)"
    lines = []
    for m in messages:
        ts_ms = m.get("id") or 0
        try:
            ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        except (OSError, ValueError):
            ts = "?"
        sender = _format_sender(m.get("fromUserId"))
        text = (m.get("text") or "").replace("\n", " ")
        lines.append(f"[{ts}] {sender}: {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------- spawning

app = FastAPI(title="YouGile webhook receiver")

_active_lock = threading.Lock()
_active_procs: Set[int] = set()

_chat_locks_mutex = threading.Lock()
_chat_busy: Dict[str, threading.Lock] = {}


def _get_chat_lock(chat_id: str) -> threading.Lock:
    with _chat_locks_mutex:
        lock = _chat_busy.get(chat_id)
        if lock is None:
            lock = threading.Lock()
            _chat_busy[chat_id] = lock
        return lock


def _build_prompt(rule: Rule, payload, chat_history_text: str, is_first_turn: bool) -> str:
    event_json = json.dumps(payload, ensure_ascii=False, indent=2)
    rendered = rule.prompt_template
    rendered = rendered.replace("{event_json}", event_json)
    rendered = rendered.replace("{chat_history}", chat_history_text)
    rendered = rendered.replace("{first_turn}", "true" if is_first_turn else "false")
    # If template uses no placeholder for history, append it so the agent still sees it.
    if "{chat_history}" not in rule.prompt_template and chat_history_text:
        rendered = (
            f"{rendered}\n\n"
            f"---\nRecent messages in this task chat "
            f"({'first turn — full history' if is_first_turn else 'new since your last reply'}):\n"
            f"{chat_history_text}\n"
        )
    return rendered


def _spawn_subprocess(rule: Rule, cmd: List[str], header: str, on_exit) -> Optional[subprocess.Popen]:
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
        return None

    pid = proc.pid
    with _active_lock:
        _active_procs.add(pid)
    log.info("rule %s: claude spawned pid=%s cmd=%s", rule.name, pid, cmd[:3] + ["..."])

    def _reap():
        rc = proc.wait()
        with _active_lock:
            _active_procs.discard(pid)
        log_fp.write(f"\n--- exit rc={rc} ts={datetime.now(timezone.utc).isoformat()} ---\n")
        log_fp.close()
        log.info("rule %s: claude pid=%s rc=%s", rule.name, pid, rc)
        try:
            on_exit(rc)
        except Exception:
            log.exception("rule %s: on_exit hook failed", rule.name)

    threading.Thread(target=_reap, daemon=True).start()
    return proc


def spawn_claude(rule: Rule, payload) -> Tuple[bool, str]:
    with _active_lock:
        if len(_active_procs) >= CLAUDE_MAX_CONCURRENT:
            return False, "global-max-concurrent"

    chat_id = extract_chat_id(payload) if rule.session_per_chat else None
    chat_lock: Optional[threading.Lock] = None
    if chat_id:
        chat_lock = _get_chat_lock(chat_id)
        if not chat_lock.acquire(blocking=False):
            log.warning("rule %s: chat %s already busy, dropping", rule.name, chat_id)
            return False, "chat-busy"

    try:
        if chat_id:
            # The claude session id IS the YouGile chat/task UUID — deterministic,
            # so we never need to map it. Whether to --resume or --session-id is
            # decided by checking claude's on-disk session file directly.
            session_id = chat_id
            session_exists = claude_session_exists(rule.workdir, session_id)
            chat_state = get_chat_state(chat_id)
            last_msg_id = chat_state.get("last_message_id")

            if not session_exists:
                # First time on this chat in this workdir (or claude state was wiped).
                # Fetch the recent history afresh; ignore any stale last_message_id.
                is_first_turn = True
                history = fetch_chat_messages(
                    chat_id, since=None, limit=CHAT_HISTORY_FIRST_TIME_LIMIT
                )
                session_flag = ["--session-id", session_id]
            else:
                is_first_turn = False
                if last_msg_id:
                    history = fetch_chat_messages(
                        chat_id, since=last_msg_id, limit=CHAT_HISTORY_DELTA_LIMIT
                    )
                else:
                    # Session exists but our state was wiped — feed recent context.
                    history = fetch_chat_messages(
                        chat_id, since=None, limit=CHAT_HISTORY_FIRST_TIME_LIMIT
                    )
                session_flag = ["--resume", session_id]

            history_text = render_chat_history(history)
            newest_msg_id = max(
                [int(m["id"]) for m in history if m.get("id")] + [last_msg_id or 0]
            )
        else:
            session_id = None
            history_text = ""
            is_first_turn = True
            newest_msg_id = None
            session_flag = []

        prompt = _build_prompt(rule, payload, history_text, is_first_turn)
        cmd = [CLAUDE_BIN, *rule.extra_args, *session_flag, "-p", prompt]

        ts = datetime.now(timezone.utc).isoformat()
        header = (
            f"\n\n=== {ts} | rule={rule.name} | chat={chat_id} | session={session_id} "
            f"| first_turn={is_first_turn} | spawn ===\n"
            f"cwd: {rule.workdir}\nbin: {CLAUDE_BIN}\nargs: {rule.extra_args + session_flag}\n"
            f"--- prompt ---\n{prompt}\n--- output ---\n"
        )

        def on_exit(rc: int) -> None:
            try:
                if chat_id and newest_msg_id:
                    update_chat_state(chat_id, last_message_id=newest_msg_id)
            finally:
                if chat_lock is not None:
                    chat_lock.release()

        proc = _spawn_subprocess(rule, cmd, header, on_exit)
        if proc is None:
            if chat_lock is not None:
                chat_lock.release()
            return False, "spawn-failed"
        return True, "ok"
    except Exception:
        log.exception("rule %s: spawn flow failed", rule.name)
        if chat_lock is not None:
            chat_lock.release()
        return False, "exception"


# ---------------------------------------------------------------- HTTP

@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "claude_bin": CLAUDE_BIN,
        "default_workdir": CLAUDE_DEFAULT_WORKDIR,
        "active_claude_procs": len(_active_procs),
        "tracked_chats": list(load_state().keys()),
        "rules": [
            {
                "name": r.name,
                "enabled": r.enabled,
                "events": sorted(r.events),
                "pattern": r.pattern.pattern if r.pattern else None,
                "allowed_sender_ids": sorted(r.allowed_sender_ids),
                "column_ids": sorted(r.column_ids),
                "workdir": r.workdir,
                "session_per_chat": r.session_per_chat,
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
    spawn_reason: Optional[str] = None
    if rule:
        ok, why = spawn_claude(rule, payload)
        spawn_reason = why
        if ok:
            fired_rule = rule.name

    event = isinstance(payload, dict) and payload.get("event")
    log.info(
        "event=%s fired=%s spawn_reason=%s match_reasons=%s",
        event, fired_rule, spawn_reason, reasons,
    )
    return {
        "ok": True,
        "fired_rule": fired_rule,
        "spawn_reason": spawn_reason,
        "match_reasons": reasons,
    }
