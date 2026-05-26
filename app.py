from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
import shlex
import subprocess
import threading
import time
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
STATE_DIR = ROOT / os.environ.get("STATE_DIR", "state")
CHATS_STATE_DIR = STATE_DIR / "chats"
CHATS_STATE_DIR.mkdir(parents=True, exist_ok=True)
ATTACHMENTS_DIR = STATE_DIR / "attachments"
ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
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
# Host used to resolve schemeless `/user-data/...` file URLs that YouGile
# inlines as `/root/#file:/user-data/...` in chat messages. Defaults to the
# YOUGILE_API_BASE origin (strip the `/api-v2` path).
_default_file_host = urllib.parse.urlparse(YOUGILE_API_BASE)
YOUGILE_FILE_BASE_URL = os.environ.get(
    "YOUGILE_FILE_BASE_URL",
    f"{_default_file_host.scheme}://{_default_file_host.netloc}" if _default_file_host.netloc else "",
).rstrip("/")

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_DEFAULT_WORKDIR = os.environ.get("CLAUDE_WORKDIR") or str(ROOT)
CLAUDE_DEFAULT_EXTRA_ARGS = shlex.split(
    os.environ.get("CLAUDE_EXTRA_ARGS", "--dangerously-skip-permissions")
)
CLAUDE_MAX_CONCURRENT = int(os.environ.get("CLAUDE_MAX_CONCURRENT", "2"))

# Retry policy for transient API errors. The Anthropic API surfaces overload /
# rate-limit failures as 5xx; the claude CLI prints them to stdout and exits
# with a non-zero code. We re-spawn the same command after a backoff.
CLAUDE_API_RETRIES = int(os.environ.get("CLAUDE_API_RETRIES", "3"))
CLAUDE_RETRY_DELAYS = [
    int(x) for x in os.environ.get("CLAUDE_RETRY_DELAYS", "30,120,300").split(",")
    if x.strip()
] or [30, 120, 300]
CLAUDE_RETRY_PATTERN = re.compile(
    r"API Error:\s*5\d\d|overloaded_error|rate_limit_error|\bOverloaded\b",
    re.IGNORECASE,
)

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

ATTACHMENTS_ENABLED = os.environ.get("ATTACHMENTS_ENABLED", "true").lower() not in ("0", "false", "no")
ATTACHMENT_MAX_BYTES = int(os.environ.get("ATTACHMENT_MAX_BYTES", str(25 * 1024 * 1024)))
ATTACHMENT_TIMEOUT_SECONDS = int(os.environ.get("ATTACHMENT_TIMEOUT_SECONDS", "30"))

RULES_FILE = ROOT / os.environ.get("RULES_FILE", "rules.toml")

# YouGile auto-disables a webhook subscription after enough failed deliveries
# (e.g. while we were down or the tunnel was unreachable). Sweep our subscriptions
# at startup and on a timer so a temporary outage doesn't permanently silence us.
WEBHOOK_URL = os.environ.get("YOUGILE_WEBHOOK_URL", "").strip()
WEBHOOK_EVENT_REGEX = os.environ.get("YOUGILE_WEBHOOK_EVENT", "(chat_message|task)-.*").strip()
WEBHOOK_ENSURE_INTERVAL_SECONDS = int(os.environ.get("WEBHOOK_ENSURE_INTERVAL_SECONDS", "900"))

# Shared formatting guidance, substituted into every prompt via {formatting}.
# Single source of truth so all spawned agents post visually-consistent messages
# into YouGile chats. Optional file — if it's missing the placeholder just gets
# stripped, so existing prompts that don't use it keep working.
_FORMATTING_FILE = ROOT / os.environ.get("FORMATTING_FILE", "prompts/_formatting.txt")
FORMATTING_BLOCK = (
    _FORMATTING_FILE.read_text(encoding="utf-8").strip()
    if _FORMATTING_FILE.exists()
    else ""
)

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
    # Skip this rule if a claude session has already been started for the
    # task's chat (i.e. state/chats/<chatId>.json exists). Lets the
    # auto-kickoff rules (e.g. task_in_progress) bow out when a human has
    # already pinged the agent in the chat via @Agent / !implement so we
    # don't pile a second LLM run on top of an existing conversation.
    skip_if_chat_known: bool = False
    # Continue the same claude session across re-mentions on the same chat.
    session_per_chat: bool = True
    # Short status reply posted into the task chat the moment this rule fires,
    # before claude finishes. Plain text; optional HTML companion.
    ack_message: Optional[str] = None
    ack_message_html: Optional[str] = None
    # Output language hint, substituted into prompts as {language}. Lets the
    # rules file declare "ru" once instead of repeating it in every prompt.
    language: Optional[str] = None
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
                skip_if_chat_known=bool(raw.get("skip_if_chat_known", False)),
                session_per_chat=bool(raw.get("session_per_chat", True)),
                ack_message=(raw.get("ack_message") or None),
                ack_message_html=(raw.get("ack_message_html") or None),
                language=(raw.get("language") or None),
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


def _api_post(path: str, body: dict) -> Optional[dict]:
    if not YOUGILE_API_KEY:
        return None
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{YOUGILE_API_BASE}{path}",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {YOUGILE_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except (urllib.error.URLError, json.JSONDecodeError):
        log.exception("API POST failed: %s", path)
        return None


def _api_put(path: str, body: dict) -> Optional[dict]:
    if not YOUGILE_API_KEY:
        return None
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{YOUGILE_API_BASE}{path}",
        data=data,
        method="PUT",
        headers={
            "Authorization": f"Bearer {YOUGILE_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except (urllib.error.URLError, json.JSONDecodeError):
        log.exception("API PUT failed: %s", path)
        return None


def _list_our_webhooks() -> List[dict]:
    raw = _api_get("/webhooks")
    if raw is None:
        return []
    subs = raw if isinstance(raw, list) else (raw.get("content") or raw.get("data") or [])
    return [s for s in subs if isinstance(s, dict) and s.get("url") == WEBHOOK_URL]


def ensure_webhook_subscription() -> bool:
    """Make sure a healthy YouGile webhook subscription points at our URL.

    Run at startup and on a timer (WEBHOOK_ENSURE_INTERVAL_SECONDS). YouGile
    disables a subscription after a streak of failed deliveries — once we are
    back up the subscription stays dead unless we touch it. We try to flip
    `disabled` back to false (preserves the id and YouGile-side counters);
    if that doesn't take, we delete and recreate. Returns True if a healthy
    subscription is in place after the call.
    """
    if not (WEBHOOK_URL and YOUGILE_API_KEY):
        return False
    ours = _list_our_webhooks()
    if any(not s.get("disabled") for s in ours):
        return True
    for s in ours:
        sub_id = s.get("id")
        if sub_id:
            _api_put(f"/webhooks/{sub_id}", {"disabled": False})
    ours = _list_our_webhooks()
    if any(not s.get("disabled") for s in ours):
        log.info("ensure_webhook: re-enabled existing subscription for %s", WEBHOOK_URL)
        return True
    for s in ours:
        sub_id = s.get("id")
        if sub_id:
            _api_put(f"/webhooks/{sub_id}", {"deleted": True})
            log.info("ensure_webhook: removed dead subscription %s", sub_id)
    created = _api_post("/webhooks", {"url": WEBHOOK_URL, "event": WEBHOOK_EVENT_REGEX})
    if created:
        log.info("ensure_webhook: created fresh subscription for %s", WEBHOOK_URL)
        return True
    log.warning("ensure_webhook: failed to provision subscription for %s", WEBHOOK_URL)
    return False


def _webhook_watcher() -> None:
    """Background daemon: keep the YouGile subscription healthy."""
    while True:
        time.sleep(WEBHOOK_ENSURE_INTERVAL_SECONDS)
        try:
            ensure_webhook_subscription()
        except Exception:
            log.exception("ensure_webhook: watcher cycle failed")


def send_chat_message(chat_id: str, text: str, text_html: Optional[str] = None) -> bool:
    """Post a chat message into a YouGile task chat. Returns True on success.
    Used for fast ack replies sent directly from the webhook handler — separate
    from messages the spawned claude posts via mcp__yougile-mcp__send_task_message.
    """
    if not chat_id or not text:
        return False
    body: Dict[str, Any] = {"text": text}
    if text_html:
        body["textHtml"] = text_html
    return _api_post(f"/chats/{chat_id}/messages", body) is not None


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

if WEBHOOK_URL:
    ensure_webhook_subscription()
    threading.Thread(target=_webhook_watcher, daemon=True).start()
else:
    log.warning("YOUGILE_WEBHOOK_URL not set — skipping subscription self-heal")


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
    if rule.skip_if_chat_known:
        chat_id = extract_chat_id(payload)
        if chat_id and chat_known(chat_id):
            return False, "chat-already-has-llm-session"
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
#
# State is sharded per-chat: one JSON file per chat under state/chats/<chatId>.json.
# Existence of a file == "we have launched claude for this chat before, the session
# is set up". This means we don't need to peek at claude's internal storage.
# Each per-chat write is atomic (write to .tmp, then rename). Concurrent writes to
# the SAME chat file are excluded by the in-memory chat_lock (drop-if-busy), so no
# global lock is needed.


def _chat_state_path(chat_id: str) -> Path:
    return CHATS_STATE_DIR / f"{chat_id}.json"


def chat_known(chat_id: str) -> bool:
    return _chat_state_path(chat_id).exists()


def get_chat_state(chat_id: str) -> Dict[str, Any]:
    p = _chat_state_path(chat_id)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.exception("chat state corrupted: %s", p)
        return {}


def update_chat_state(chat_id: str, **fields: Any) -> Dict[str, Any]:
    p = _chat_state_path(chat_id)
    existing = get_chat_state(chat_id)
    existing.update(fields)
    existing["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return dict(existing)


def list_known_chats() -> List[str]:
    return sorted(p.stem for p in CHATS_STATE_DIR.glob("*.json"))


def _migrate_legacy_state() -> None:
    """One-time migration from the old monolithic state/sessions.json layout."""
    legacy = STATE_DIR / "sessions.json"
    if not legacy.exists():
        return
    try:
        data = json.loads(legacy.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    migrated = 0
    for chat_id, entry in (data or {}).items():
        if not isinstance(entry, dict) or _chat_state_path(chat_id).exists():
            continue
        new_entry = {
            k: entry[k]
            for k in ("last_message_id", "updated_at", "first_seen_at")
            if k in entry
        }
        update_chat_state(chat_id, **new_entry)
        migrated += 1
    legacy.rename(legacy.with_suffix(".json.migrated"))
    log.info("migrated %d chat(s) from legacy sessions.json", migrated)


_migrate_legacy_state()


# ---------------------------------------------------------------- attachments
#
# YouGile embeds chat files inline as "/root/#file:<url>" inside the message
# text (see yougile-mcp/src/tools/task-chat.ts). We download each unique URL
# once per chat into state/attachments/<chatId>/ and rewrite occurrences in
# rendered history to "[image: <abs-path>]" / "[file: <abs-path>]" so the
# claude agent can open them with its Read tool.

FILE_MARKER_RE = re.compile(r"/root/#file:(\S+)")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".heic", ".heif", ".avif"}


def _safe_filename(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name[:80] or "file"


def _is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTS


def _attachment_dir(chat_id: Optional[str]) -> Path:
    sub = chat_id or "_global"
    d = ATTACHMENTS_DIR / sub
    d.mkdir(parents=True, exist_ok=True)
    return d


def _download_attachment(url: str, chat_id: Optional[str]) -> Optional[Path]:
    """Download `url` into the chat's attachment dir. Idempotent — re-uses
    files when a previous run already fetched the same URL. Returns the local
    path on success, None on any failure.
    """
    if not ATTACHMENTS_ENABLED:
        return None
    # YouGile sometimes inlines preview thumbnails with the query string
    # double-encoded into the path component, e.g.
    #   `.../image.png%3Fpreviews%5B%5D%3D-256-preview%40256x128`
    # i.e. `image.png?previews[]=-256-preview@256x128` re-encoded as a
    # literal filename. The server treats that as an unknown file and
    # 404s. Decode once and drop the (now real) query string so we fetch
    # the original full-resolution file instead.
    if "%3F" in url or "%3f" in url:
        url = urllib.parse.unquote(url).split("?", 1)[0]
    parsed = urllib.parse.urlparse(url)
    # YouGile inlines uploaded files as `/root/#file:/user-data/...` — the
    # extracted URL is a server-relative path. Resolve it against the YouGile
    # origin so the download actually fires.
    if parsed.scheme not in ("http", "https"):
        if YOUGILE_FILE_BASE_URL and url.startswith("/"):
            url = f"{YOUGILE_FILE_BASE_URL}{url}"
            parsed = urllib.parse.urlparse(url)
        else:
            return None
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    raw_name = _safe_filename(Path(parsed.path).name or digest)
    chat_dir = _attachment_dir(chat_id)

    # If we already downloaded this URL, find the matching file by prefix.
    for existing in chat_dir.glob(f"{digest}_*"):
        if existing.is_file() and existing.stat().st_size > 0:
            return existing

    headers: Dict[str, str] = {"User-Agent": "yougile-webhook/1.0"}
    if YOUGILE_API_KEY and "yougile" in (parsed.netloc or ""):
        headers["Authorization"] = f"Bearer {YOUGILE_API_KEY}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=ATTACHMENT_TIMEOUT_SECONDS) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            ext = Path(raw_name).suffix
            if not ext and ctype:
                guessed = mimetypes.guess_extension(ctype)
                if guessed:
                    raw_name = f"{Path(raw_name).stem}{guessed}"
            dest = chat_dir / f"{digest}_{raw_name}"
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            written = 0
            with tmp.open("wb") as f:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > ATTACHMENT_MAX_BYTES:
                        raise IOError(
                            f"attachment exceeds ATTACHMENT_MAX_BYTES "
                            f"({ATTACHMENT_MAX_BYTES} bytes)"
                        )
                    f.write(chunk)
            tmp.replace(dest)
            log.info("downloaded attachment chat=%s url=%s -> %s (%d bytes)",
                     chat_id, url, dest, written)
            return dest
    except Exception as e:
        log.warning("attachment download failed chat=%s url=%s: %s", chat_id, url, e)
        try:
            tmp_path = chat_dir / f"{digest}_{raw_name}.tmp"
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return None


def _extract_attachment_urls(message: Any) -> List[str]:
    """Pull every /root/#file:<url> reference out of a message dict — checks
    the text field plus any nested chunks the YouGile editor inlines.
    """
    urls: List[str] = []
    if not isinstance(message, dict):
        return urls
    text = message.get("text") or ""
    if isinstance(text, str):
        urls.extend(FILE_MARKER_RE.findall(text))
    text_html = message.get("textHtml") or ""
    if isinstance(text_html, str):
        urls.extend(FILE_MARKER_RE.findall(text_html))
    chunks = (((message.get("properties") or {}).get("params") or {}).get("chunks")) or []
    if isinstance(chunks, list):
        for c in chunks:
            if not isinstance(c, dict):
                continue
            for v in (c.get("replacement"), (c.get("data") or {}).get("url")):
                if isinstance(v, str):
                    urls.extend(FILE_MARKER_RE.findall(v))
    seen: Set[str] = set()
    out: List[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _render_attachment_marker(path: Path) -> str:
    kind = "image" if _is_image(path) else "file"
    return f"[{kind}: {path}]"


def _annotate_text_with_attachments(
    text: str, url_to_path: Dict[str, Path]
) -> Tuple[str, List[Path]]:
    """Replace each /root/#file:<url> in `text` with a `[image:|file: <path>]`
    marker. Returns the rewritten text and the list of locally-downloaded
    paths actually referenced.
    """
    referenced: List[Path] = []

    def _sub(m: re.Match) -> str:
        url = m.group(1)
        path = url_to_path.get(url)
        if path is None:
            return m.group(0)
        referenced.append(path)
        return _render_attachment_marker(path)

    return FILE_MARKER_RE.sub(_sub, text), referenced


def download_message_attachments(
    message: Any, chat_id: Optional[str]
) -> Dict[str, Path]:
    """Download every attachment referenced by `message`. Returns
    {url: local_path} for everything that succeeded.
    """
    if not ATTACHMENTS_ENABLED:
        return {}
    out: Dict[str, Path] = {}
    for url in _extract_attachment_urls(message):
        path = _download_attachment(url, chat_id)
        if path is not None:
            out[url] = path
    return out


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


def render_chat_history(
    messages: List[dict], chat_id: Optional[str] = None
) -> Tuple[str, List[Path]]:
    """Render messages as text. Downloads any `/root/#file:<url>` attachments
    to state/attachments/<chatId>/ and rewrites them inline as
    `[image: <abs-path>]` / `[file: <abs-path>]`. Returns (rendered_text,
    sorted list of unique local attachment paths referenced anywhere in the
    rendered history).
    """
    if not messages:
        return "(no prior messages)", []
    lines: List[str] = []
    all_paths: List[Path] = []
    seen_paths: Set[str] = set()
    for m in messages:
        ts_ms = m.get("id") or 0
        try:
            ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        except (OSError, ValueError):
            ts = "?"
        sender = _format_sender(m.get("fromUserId"))
        text = m.get("text") or ""
        url_to_path = download_message_attachments(m, chat_id)
        text, referenced = _annotate_text_with_attachments(text, url_to_path)
        # Append any attachments that weren't inlined in text (e.g. attached
        # via chunks only) so the agent still sees them on this line.
        extras = [p for u, p in url_to_path.items() if p not in referenced]
        for p in extras:
            text = f"{text} {_render_attachment_marker(p)}".strip()
        for p in list(url_to_path.values()):
            key = str(p)
            if key not in seen_paths:
                seen_paths.add(key)
                all_paths.append(p)
        text = text.replace("\n", " ")
        lines.append(f"[{ts}] {sender}: {text}")
    return "\n".join(lines), all_paths


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


def _build_prompt(
    rule: Rule,
    payload,
    chat_history_text: str,
    is_first_turn: bool,
    attachment_paths: List[Path],
) -> str:
    event_json = json.dumps(payload, ensure_ascii=False, indent=2)
    rendered = rule.prompt_template
    rendered = rendered.replace("{event_json}", event_json)
    rendered = rendered.replace("{chat_history}", chat_history_text)
    rendered = rendered.replace("{first_turn}", "true" if is_first_turn else "false")
    rendered = rendered.replace("{formatting}", FORMATTING_BLOCK)
    rendered = rendered.replace("{language}", rule.language or "")
    # If the template doesn't reference {formatting} explicitly, append it so
    # every spawned agent still sees the rules — guarantees consistent output.
    if "{formatting}" not in rule.prompt_template and FORMATTING_BLOCK:
        rendered = f"{rendered}\n\n---\n{FORMATTING_BLOCK}\n"
    # If template uses no placeholder for history, append it so the agent still sees it.
    if "{chat_history}" not in rule.prompt_template and chat_history_text:
        rendered = (
            f"{rendered}\n\n"
            f"---\nRecent messages in this task chat "
            f"({'first turn — full history' if is_first_turn else 'new since your last reply'}):\n"
            f"{chat_history_text}\n"
        )
    if attachment_paths:
        listed = "\n".join(f"- {_render_attachment_marker(p)}" for p in attachment_paths)
        rendered = (
            f"{rendered}\n\n"
            f"---\nAttachments referenced above are saved locally — open them "
            f"with your Read tool to view their contents:\n{listed}\n"
        )
    return rendered


def _spawn_subprocess(
    rule: Rule,
    cmd: List[str],
    header: str,
    on_exit,
    attempt: int = 1,
) -> Optional[subprocess.Popen]:
    log_fp = CLAUDE_LOG.open("a", encoding="utf-8")
    if attempt == 1:
        log_fp.write(header)
    else:
        log_fp.write(
            f"\n--- retry attempt {attempt}/{CLAUDE_API_RETRIES} "
            f"ts={datetime.now(timezone.utc).isoformat()} ---\n"
        )
    log_fp.flush()
    output_start = log_fp.tell()
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
        log.exception("rule %s: failed to spawn claude (attempt %d)", rule.name, attempt)
        log_fp.close()
        # On first-attempt spawn failure the caller cleans up (chat_lock etc.).
        # On retry there is no caller waiting — make sure on_exit still fires.
        if attempt > 1:
            try:
                on_exit(-1)
            except Exception:
                log.exception("rule %s: on_exit hook failed", rule.name)
        return None

    pid = proc.pid
    with _active_lock:
        _active_procs.add(pid)
    log.info(
        "rule %s: claude spawned pid=%s attempt=%d cmd=%s",
        rule.name, pid, attempt, cmd[:3] + ["..."],
    )

    def _reap():
        rc = proc.wait()
        with _active_lock:
            _active_procs.discard(pid)
        log_fp.write(
            f"\n--- exit rc={rc} attempt={attempt} "
            f"ts={datetime.now(timezone.utc).isoformat()} ---\n"
        )
        log_fp.flush()
        end_pos = log_fp.tell()
        log_fp.close()
        log.info("rule %s: claude pid=%s attempt=%d rc=%s", rule.name, pid, attempt, rc)

        if rc != 0 and attempt < CLAUDE_API_RETRIES and _output_is_transient(output_start, end_pos):
            delay = CLAUDE_RETRY_DELAYS[min(attempt - 1, len(CLAUDE_RETRY_DELAYS) - 1)]
            log.warning(
                "rule %s: transient API error (rc=%s) — retrying in %ds (attempt %d/%d)",
                rule.name, rc, delay, attempt + 1, CLAUDE_API_RETRIES,
            )

            def _delayed_retry():
                time.sleep(delay)
                _spawn_subprocess(rule, cmd, header, on_exit, attempt=attempt + 1)

            threading.Thread(target=_delayed_retry, daemon=True).start()
            return

        try:
            on_exit(rc)
        except Exception:
            log.exception("rule %s: on_exit hook failed", rule.name)

    threading.Thread(target=_reap, daemon=True).start()
    return proc


def _output_is_transient(start: int, end: int) -> bool:
    """Read this attempt's slice of CLAUDE_LOG and check for an Anthropic 5xx /
    overload / rate-limit signature."""
    if end <= start:
        return False
    try:
        with CLAUDE_LOG.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(start)
            output = f.read(end - start)
    except Exception:
        log.exception("failed to read claude.log slice for retry decision")
        return False
    return bool(CLAUDE_RETRY_PATTERN.search(output))


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
            # so we never need to map it. "Have we already started a session for
            # this chat?" is answered solely by whether state/chats/<chatId>.json
            # exists. No peeking into claude's internal storage.
            session_id = chat_id
            known = chat_known(chat_id)
            chat_state = get_chat_state(chat_id)
            last_msg_id = chat_state.get("last_message_id")

            if not known:
                is_first_turn = True
                history = fetch_chat_messages(
                    chat_id, since=None, limit=CHAT_HISTORY_FIRST_TIME_LIMIT
                )
                session_flag = ["--session-id", session_id]
                # Mark the chat as known BEFORE spawn so a crash mid-launch
                # doesn't make us double-fire --session-id on retry.
                update_chat_state(
                    chat_id,
                    first_seen_at=datetime.now(timezone.utc).isoformat(),
                )
            else:
                is_first_turn = False
                history = fetch_chat_messages(
                    chat_id,
                    since=last_msg_id,
                    limit=CHAT_HISTORY_DELTA_LIMIT,
                )
                session_flag = ["--resume", session_id]

            history_text, attachment_paths = render_chat_history(history, chat_id)
            newest_msg_id = max(
                [int(m["id"]) for m in history if m.get("id")] + [last_msg_id or 0]
            )
        else:
            session_id = None
            history_text = ""
            is_first_turn = True
            newest_msg_id = None
            session_flag = []
            attachment_paths = []

        # Webhook can race ahead of /chats/<id>/messages — if the trigger
        # message itself carries attachments we haven't seen in history yet,
        # pull them in so the agent doesn't miss the very file that prompted
        # this run.
        trigger_message = payload.get("payload") if isinstance(payload, dict) else None
        trigger_urls = download_message_attachments(trigger_message, chat_id)
        seen_paths = {str(p) for p in attachment_paths}
        for p in trigger_urls.values():
            if str(p) not in seen_paths:
                seen_paths.add(str(p))
                attachment_paths.append(p)

        prompt = _build_prompt(rule, payload, history_text, is_first_turn, attachment_paths)
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

        # Fast ack — let the user see "I'm on it" without waiting for claude
        # to finish. Only fire AFTER claude was actually spawned, so a failed
        # launch never leaves an orphan "taking it" message in the chat. The
        # session id (== chat_id) is appended so the user can copy it from the
        # task and resume the same session from a terminal:
        #     claude --resume <id>
        # In a thread so urllib doesn't add latency to the webhook response.
        if chat_id and rule.ack_message and session_id:
            ack_chat_id = chat_id
            ack_session = session_id
            ack_text = f"{rule.ack_message}\n\nsession: {ack_session}"
            if rule.ack_message_html:
                ack_html = (
                    f"{rule.ack_message_html}"
                    f"<p>session: <code>{ack_session}</code></p>"
                )
            else:
                ack_html = (
                    f"<p>{rule.ack_message}</p>"
                    f"<p>session: <code>{ack_session}</code></p>"
                )
            rule_name = rule.name

            def _send_ack() -> None:
                try:
                    ok = send_chat_message(ack_chat_id, ack_text, ack_html)
                    log.info("rule %s: ack sent chat=%s ok=%s", rule_name, ack_chat_id, ok)
                except Exception:
                    log.exception("rule %s: ack send failed chat=%s", rule_name, ack_chat_id)

            threading.Thread(target=_send_ack, daemon=True).start()

        return True, "ok"
    except Exception:
        log.exception("rule %s: spawn flow failed", rule.name)
        if chat_lock is not None:
            chat_lock.release()
        return False, "exception"


# ---------------------------------------------------------------- HTTP

@app.get("/healthz")
def healthz():
    subs = _list_our_webhooks() if WEBHOOK_URL else []
    return {
        "ok": True,
        "claude_bin": CLAUDE_BIN,
        "default_workdir": CLAUDE_DEFAULT_WORKDIR,
        "active_claude_procs": len(_active_procs),
        "tracked_chats": list_known_chats(),
        "webhook_subscriptions": [
            {
                "id": s.get("id"),
                "disabled": bool(s.get("disabled")),
                "failures_since_last_success": s.get("failuresSinceLastSuccess"),
                "last_success": s.get("lastSuccess"),
            }
            for s in subs
        ],
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
