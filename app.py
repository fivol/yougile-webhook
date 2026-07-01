from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import shlex
import signal
import socket
import subprocess
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
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

# --- YouGile API retry policy ------------------------------------------------
# A VPN flapping on this host, a brief YouGile outage, or a TLS handshake timing
# out should never lose a 👍, an ack, or a chat-history read. Each HTTP attempt
# is bounded by YOUGILE_API_TIMEOUT; on a *transient* failure (network error,
# timeout, HTTP 429, or 5xx) we retry with capped exponential backoff. A 4xx is
# a permanent client error (rejected reaction emoji, stale id, bad key) and is
# never retried — that would spin forever.
#
# Data-plane calls (reaction, ack, history fetch, message post) default to
# infinite retries (YOUGILE_API_MAX_RETRIES=0) so the flow survives any blip.
# Boot/control-plane calls (directory load, webhook (de)registration) pass a
# small finite budget so a dead API at startup can't hang the import, and the
# watcher just retries on its next cycle.
YOUGILE_API_TIMEOUT = int(os.environ.get("YOUGILE_API_TIMEOUT", "30"))
YOUGILE_API_MAX_RETRIES = int(os.environ.get("YOUGILE_API_MAX_RETRIES", "0"))  # 0 = infinite
YOUGILE_API_BOOT_RETRIES = int(os.environ.get("YOUGILE_API_BOOT_RETRIES", "2"))
# Watcher-driven ensure() runs off the request path, so it can afford a bigger
# budget than the bounded boot retries — a flaky window shouldn't make it give
# up and go silent for a whole interval.
YOUGILE_API_WATCHER_RETRIES = int(os.environ.get("YOUGILE_API_WATCHER_RETRIES", "5"))
YOUGILE_API_RETRY_BASE_DELAY = float(os.environ.get("YOUGILE_API_RETRY_BASE_DELAY", "1"))
YOUGILE_API_RETRY_MAX_DELAY = float(os.environ.get("YOUGILE_API_RETRY_MAX_DELAY", "30"))

# --- Force IPv4 for all outbound connections --------------------------------
# The #1 cause of "SSL handshake timed out" on an otherwise-healthy host is a
# broken/half-open IPv6 route: getaddrinfo returns an AAAA record first, urllib
# tries it, and the TLS handshake stalls on the dead route until the full
# timeout — while `curl` (which does Happy-Eyeballs, racing v4 and v6) connects
# instantly. urllib has no Happy-Eyeballs, so we drop AAAA results and use IPv4
# only. We keep any explicit AF_INET6 lookups and fall back to the original
# result set if a host has no A record, so nothing that genuinely needs IPv6
# breaks. Toggle off with NETWORK_FORCE_IPV4=false.
NETWORK_FORCE_IPV4 = os.environ.get("NETWORK_FORCE_IPV4", "true").lower() not in ("0", "false", "no")
if NETWORK_FORCE_IPV4:
    _orig_getaddrinfo = socket.getaddrinfo

    def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        results = _orig_getaddrinfo(host, port, family, type, proto, flags)
        if family in (0, socket.AF_UNSPEC):
            ipv4 = [r for r in results if r[0] == socket.AF_INET]
            if ipv4:
                return ipv4
        return results

    socket.getaddrinfo = _ipv4_only_getaddrinfo
    log.info("network: forcing IPv4 for outbound connections (NETWORK_FORCE_IPV4=true)")

# --- Event-loop watchdog ----------------------------------------------------
# launchd KeepAlive restarts the process if it EXITS, but not if the asyncio
# loop wedges (the failure mode that once left us silently dead for hours). A
# heartbeat task bumps a monotonic timestamp on the loop; a daemon thread
# force-exits the process if that timestamp goes stale, letting launchd bring
# up a fresh one. The threshold is deliberately generous so a momentarily busy
# loop is never mistaken for a wedged one. Set the timeout to 0 to disable.
LOOP_WATCHDOG_TIMEOUT_SECONDS = int(os.environ.get("LOOP_WATCHDOG_TIMEOUT_SECONDS", "180"))
LOOP_WATCHDOG_CHECK_SECONDS = int(os.environ.get("LOOP_WATCHDOG_CHECK_SECONDS", "15"))
_loop_heartbeat = time.monotonic()

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_DEFAULT_WORKDIR = os.environ.get("CLAUDE_WORKDIR") or str(ROOT)
CLAUDE_DEFAULT_EXTRA_ARGS = shlex.split(
    os.environ.get("CLAUDE_EXTRA_ARGS", "--dangerously-skip-permissions")
)
CLAUDE_MAX_CONCURRENT = int(os.environ.get("CLAUDE_MAX_CONCURRENT", "10"))

# When a new @Agent message lands in a chat whose turn is STILL running, we
# interrupt the in-flight turn (kill its process group) and immediately start a
# fresh turn that --resumes the same session — so the agent sees the new
# instruction and can continue or change direction. After the initial SIGTERM
# we wait this many 0.1s ticks (default 30 → ~3s, lets claude flush its session
# transcript) before escalating to SIGKILL.
INTERRUPT_SIGKILL_GRACE_TICKS = int(os.environ.get("INTERRUPT_SIGKILL_GRACE_TICKS", "30"))

# Posted into a task chat when a turn can't start because the global
# concurrency cap (CLAUDE_MAX_CONCURRENT) is saturated by OTHER chats — so the
# user gets a clear "try again shortly" instead of a silent drop. Plain text +
# optional HTML, both overridable via env. Empty text disables the notice.
CONCURRENCY_BUSY_MESSAGE = os.environ.get(
    "CONCURRENCY_BUSY_MESSAGE",
    "I'm at capacity right now — too many tasks are running in parallel. "
    "Please mention @Agent again in a couple of minutes and I'll pick this up.",
).strip()
CONCURRENCY_BUSY_MESSAGE_HTML = os.environ.get("CONCURRENCY_BUSY_MESSAGE_HTML", "").strip() or None

# Prepended to the prompt when this turn is replacing a turn we just
# interrupted, so the resumed agent treats the newest chat message as a live
# course-correction rather than a routine follow-up.
INTERRUPT_NOTICE = (
    "⚠️ YOU WERE INTERRUPTED. While you were still working on the previous "
    "instruction in this chat, the user sent a NEW message (the most recent "
    "message in the chat history below) — on purpose, to add to or REDIRECT "
    "your current work. Before anything else:\n"
    "  1. Re-read the latest user message(s) and treat them as the current, "
    "authoritative instruction.\n"
    "  2. Check your workspace / git state — the previous turn was cut off "
    "mid-run and may have left uncommitted edits or a half-finished step.\n"
    "  3. Then continue, extend, or change direction as the new message "
    "requires, and proceed through your normal flow (ack → work → final reply)."
)

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
ATTACHMENT_TIMEOUT_SECONDS = int(os.environ.get("ATTACHMENT_TIMEOUT_SECONDS", "60"))
# Retry a transient attachment download (network error, timeout, 5xx) a few
# times before giving up — a VPN blip shouldn't cost the agent a screenshot.
ATTACHMENT_MAX_RETRIES = int(os.environ.get("ATTACHMENT_MAX_RETRIES", "3"))

# Reaction dropped on the trigger message the instant it matches a rule — a
# fast "received" signal that fires BEFORE the agent spawns and before any ack
# reply. YouGile only accepts a fixed reaction set
# (👍 👎 👏 🙂 😀 😕 🎉 ❤ 🚀 ✔) — 👀 (eyes) is rejected with HTTP 400. Set to an
# empty string to disable reacting entirely.
ACK_REACTION_EMOJI = os.environ.get("ACK_REACTION_EMOJI", "👍").strip()

RULES_FILE = ROOT / os.environ.get("RULES_FILE", "rules.toml")

# YouGile auto-disables a webhook subscription after enough failed deliveries
# (e.g. while we were down or the tunnel was unreachable). Sweep our subscriptions
# at startup and on a timer so a temporary outage doesn't permanently silence us.
WEBHOOK_URL = os.environ.get("YOUGILE_WEBHOOK_URL", "").strip()
WEBHOOK_EVENT_REGEX = os.environ.get("YOUGILE_WEBHOOK_EVENT", "(chat_message|task)-.*").strip()
WEBHOOK_ENSURE_INTERVAL_SECONDS = int(os.environ.get("WEBHOOK_ENSURE_INTERVAL_SECONDS", "900"))
# After a failed ensure cycle (couldn't reach YouGile / couldn't revive the
# subscription) retry this soon instead of waiting the full interval, so we
# recover from an outage in minutes.
WEBHOOK_ENSURE_RETRY_SECONDS = int(os.environ.get("WEBHOOK_ENSURE_RETRY_SECONDS", "60"))

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

def _api_request(
    method: str,
    path: str,
    body: Optional[dict] = None,
    *,
    retries: Optional[int] = None,
) -> Optional[dict]:
    """Call the YouGile API, retrying transient failures with capped backoff.

    Each attempt is bounded by YOUGILE_API_TIMEOUT. Network-level errors (DNS,
    connection reset, TLS-handshake / read timeout — what a flapping VPN looks
    like), HTTP 429, and 5xx are retried; `retries` caps the count (None ->
    YOUGILE_API_MAX_RETRIES, 0 -> retry forever). A 4xx is a permanent client
    error and returns None at once — retrying it would spin. Returns the parsed
    JSON ({} for an empty body) on success, None on a permanent error or once
    the retry budget is exhausted.
    """
    if not YOUGILE_API_KEY:
        return None
    data = (
        json.dumps(body, ensure_ascii=False).encode("utf-8")
        if body is not None
        else None
    )
    headers = {"Authorization": f"Bearer {YOUGILE_API_KEY}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{YOUGILE_API_BASE}{path}", data=data, method=method, headers=headers
    )

    max_retries = YOUGILE_API_MAX_RETRIES if retries is None else retries
    attempt = 0
    delay = YOUGILE_API_RETRY_BASE_DELAY
    while True:
        attempt += 1
        try:
            with urllib.request.urlopen(req, timeout=YOUGILE_API_TIMEOUT) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            # We got an HTTP response. 4xx (except 429) is permanent — a rejected
            # emoji, a stale id, a bad key — so don't waste retries on it.
            if exc.code != 429 and 400 <= exc.code < 500:
                log.warning(
                    "API %s %s -> HTTP %s (permanent, not retrying)", method, path, exc.code
                )
                return None
            reason: Any = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
        except json.JSONDecodeError:
            # Got a body but it isn't JSON — not a connectivity issue, retrying
            # won't help.
            log.exception("API %s %s: malformed JSON response", method, path)
            return None

        if max_retries and attempt > max_retries:
            log.warning(
                "API %s %s: giving up after %d attempt(s) (%s)", method, path, attempt, reason
            )
            return None
        log.warning(
            "API %s %s: transient failure (%s) — retry %d in %.0fs",
            method, path, reason, attempt, delay,
        )
        time.sleep(delay)
        delay = min(delay * 2, YOUGILE_API_RETRY_MAX_DELAY)


def _api_get(path: str, *, retries: Optional[int] = None) -> Optional[dict]:
    return _api_request("GET", path, retries=retries)


def _api_post(path: str, body: dict, *, retries: Optional[int] = None) -> Optional[dict]:
    return _api_request("POST", path, body, retries=retries)


def _api_put(path: str, body: dict, *, retries: Optional[int] = None) -> Optional[dict]:
    return _api_request("PUT", path, body, retries=retries)


# Last-known subscription state, refreshed by ensure_webhook_subscription() so
# /healthz can report it instantly without a live (blocking) API round-trip.
_webhook_status_lock = threading.Lock()
_webhook_status: Dict[str, Any] = {
    "subscriptions": [],
    "healthy": None,      # None = never checked yet
    "checked_at": None,   # ISO timestamp of the last successful /webhooks read
    "last_ensure_ok": None,
}


def _store_webhook_status(subs: List[dict], *, reachable: bool) -> None:
    with _webhook_status_lock:
        if reachable:
            _webhook_status["subscriptions"] = [
                {
                    "id": s.get("id"),
                    "disabled": bool(s.get("disabled")),
                    "failures_since_last_success": s.get("failuresSinceLastSuccess"),
                    "last_success": s.get("lastSuccess"),
                }
                for s in subs
            ]
            _webhook_status["healthy"] = any(not s.get("disabled") for s in subs)
            _webhook_status["checked_at"] = datetime.now(timezone.utc).isoformat()


def _list_our_webhooks(*, retries: Optional[int] = None) -> List[dict]:
    budget = YOUGILE_API_BOOT_RETRIES if retries is None else retries
    raw = _api_get("/webhooks", retries=budget)
    if raw is None:
        return []
    subs = raw if isinstance(raw, list) else (raw.get("content") or raw.get("data") or [])
    ours = [s for s in subs if isinstance(s, dict) and s.get("url") == WEBHOOK_URL]
    _store_webhook_status(ours, reachable=True)
    return ours


def ensure_webhook_subscription(*, retries: Optional[int] = None) -> bool:
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
    budget = YOUGILE_API_BOOT_RETRIES if retries is None else retries

    def _done(ok: bool) -> bool:
        with _webhook_status_lock:
            _webhook_status["last_ensure_ok"] = ok
        return ok

    ours = _list_our_webhooks(retries=budget)
    if any(not s.get("disabled") for s in ours):
        return _done(True)
    for s in ours:
        sub_id = s.get("id")
        if sub_id:
            _api_put(f"/webhooks/{sub_id}", {"disabled": False}, retries=budget)
    ours = _list_our_webhooks(retries=budget)
    if any(not s.get("disabled") for s in ours):
        log.info("ensure_webhook: re-enabled existing subscription for %s", WEBHOOK_URL)
        return _done(True)
    for s in ours:
        sub_id = s.get("id")
        if sub_id:
            _api_put(f"/webhooks/{sub_id}", {"deleted": True}, retries=budget)
            log.info("ensure_webhook: removed dead subscription %s", sub_id)
    created = _api_post(
        "/webhooks", {"url": WEBHOOK_URL, "event": WEBHOOK_EVENT_REGEX},
        retries=budget,
    )
    if created:
        log.info("ensure_webhook: created fresh subscription for %s", WEBHOOK_URL)
        return _done(True)
    log.warning("ensure_webhook: failed to provision subscription for %s", WEBHOOK_URL)
    return _done(False)


def _webhook_watcher() -> None:
    """Background daemon: keep the YouGile subscription healthy.

    Self-healing cadence: when the subscription is confirmed healthy we relax to
    the long interval; when a cycle can't reach YouGile or can't provision the
    subscription, we retry again soon (WEBHOOK_ENSURE_RETRY_SECONDS) so recovery
    from an outage takes minutes, not a full interval. The ensure runs off the
    request path with a generous retry budget.
    """
    while True:
        try:
            ok = ensure_webhook_subscription(retries=YOUGILE_API_WATCHER_RETRIES)
        except Exception:
            log.exception("ensure_webhook: watcher cycle failed")
            ok = False
        time.sleep(WEBHOOK_ENSURE_INTERVAL_SECONDS if ok else WEBHOOK_ENSURE_RETRY_SECONDS)


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


def react_to_message(chat_id: str, message_id: str, emoji: str) -> bool:
    """Set the API-token user's reaction on a chat message. Returns True on success.

    YouGile's reaction set is fixed (👍 👎 👏 🙂 😀 😕 🎉 ❤ 🚀 ✔); anything else
    (e.g. 👀) is rejected with HTTP 400. The `react` body field is an ARRAY that
    REPLACES this user's whole reaction list — despite the OpenAPI spec typing it
    as a string — so a single-element array sets exactly one reaction and is
    idempotent on webhook re-delivery. Used as the fast "received" signal the
    moment a rule matches, before the agent is even spawned.
    """
    if not (chat_id and message_id and emoji):
        return False
    return _api_put(f"/chats/{chat_id}/messages/{message_id}", {"react": [emoji]}) is not None


USERS_BY_ID: Dict[str, Dict[str, Any]] = {}
USERS_BY_EMAIL: Dict[str, str] = {}
COLUMNS_BY_NAME: Dict[str, Set[str]] = {}


def refresh_directories() -> None:
    USERS_BY_ID.clear()
    USERS_BY_EMAIL.clear()
    COLUMNS_BY_NAME.clear()
    users = _api_get("/users?limit=1000", retries=YOUGILE_API_BOOT_RETRIES) or {}
    for u in users.get("content", []):
        uid = u["id"]
        USERS_BY_ID[uid] = u
        em = (u.get("email") or "").lower()
        if em:
            USERS_BY_EMAIL[em] = uid
    columns = _api_get("/columns?limit=1000", retries=YOUGILE_API_BOOT_RETRIES) or {}
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


def extract_message_id(payload) -> Optional[str]:
    """Numeric chat-message id (a millisecond timestamp) for chat_message-* events.

    It lives at payload.payload.id and is returned as a string for the API path.
    Returns None for task/column events, whose payload.id is a UUID rather than a
    digit string — so reacting naturally no-ops on anything but a chat message.
    """
    if not isinstance(payload, dict):
        return None
    inner = payload.get("payload")
    if isinstance(inner, dict):
        mid = inner.get("id")
        if isinstance(mid, int) or (isinstance(mid, str) and mid.isdigit()):
            return str(mid)
    return None


# YouGile delivers webhooks at-least-once: when our 200 is slow — e.g. a first
# turn that synchronously fetches the full chat history before responding — it
# re-sends the *same* chat_message-created (identical id) several times within
# seconds. A duplicate must never reach spawn_claude again: the re-delivery is
# seen as a "new instruction" and interrupts the in-flight turn. If that turn is
# the first one (creating the session via --session-id), SIGTERM kills it before
# the session is persisted, and every later --resume then dies with "No
# conversation found" — the chat reacts 👍 but can never answer. So we drop
# re-deliveries by id here. Bounded in-memory LRU is enough: duplicates arrive
# within seconds, long before a restart would clear the cache.
_DEDUP_CACHE_SIZE = 2048
_seen_msg_ids: "OrderedDict[str, None]" = OrderedDict()
_seen_msg_ids_lock = threading.Lock()


def is_duplicate_delivery(payload) -> bool:
    """True if this exact chat message was already dispatched (records it on the
    first sighting). Returns False for events without a numeric message id
    (task/column events), whose idempotency is handled by their own match rules
    — so they are never deduped here."""
    mid = extract_message_id(payload)
    if mid is None:
        return False
    event = payload.get("event") if isinstance(payload, dict) else None
    key = f"{event}|{extract_chat_id(payload)}|{mid}"
    with _seen_msg_ids_lock:
        if key in _seen_msg_ids:
            _seen_msg_ids.move_to_end(key)
            return True
        _seen_msg_ids[key] = None
        while len(_seen_msg_ids) > _DEDUP_CACHE_SIZE:
            _seen_msg_ids.popitem(last=False)
        return False


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
    # Decode URL-escaped basename ONCE before sanitizing — otherwise paths
    # like `%D0%A1%D0%BD…` (Cyrillic filename) blow up to 80+ chars of
    # underscores and the `.png` extension falls off in truncation. After
    # `unquote` we get the literal filename (Cyrillic or otherwise) and
    # `_safe_filename` replaces only the non-ASCII chars, leaving the
    # extension and any date markers intact.
    raw_basename = urllib.parse.unquote(Path(parsed.path).name) or digest
    raw_name = _safe_filename(raw_basename)
    chat_dir = _attachment_dir(chat_id)

    # If we already downloaded this URL, find the matching file by prefix.
    for existing in chat_dir.glob(f"{digest}_*"):
        if existing.is_file() and existing.stat().st_size > 0:
            return existing

    headers: Dict[str, str] = {"User-Agent": "yougile-webhook/1.0"}
    if YOUGILE_API_KEY and "yougile" in (parsed.netloc or ""):
        headers["Authorization"] = f"Bearer {YOUGILE_API_KEY}"
    req = urllib.request.Request(url, headers=headers)

    def _cleanup_tmp() -> None:
        try:
            for leftover in chat_dir.glob(f"{digest}_*.tmp"):
                leftover.unlink(missing_ok=True)
        except Exception:
            pass

    # Same transient-vs-permanent split as the API layer: retry network errors,
    # timeouts and 5xx with capped backoff; give up at once on a 4xx (stale
    # preview URL, 403) or an over-size file, where retrying can't help.
    attempt = 0
    delay = YOUGILE_API_RETRY_BASE_DELAY
    while True:
        attempt += 1
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
                            raise ValueError(
                                f"attachment exceeds ATTACHMENT_MAX_BYTES "
                                f"({ATTACHMENT_MAX_BYTES} bytes)"
                            )
                        f.write(chunk)
                tmp.replace(dest)
                log.info("downloaded attachment chat=%s url=%s -> %s (%d bytes)",
                         chat_id, url, dest, written)
                return dest
        except urllib.error.HTTPError as e:  # HTTPError is-a URLError/OSError — catch first
            _cleanup_tmp()
            if 400 <= e.code < 500:
                log.warning("attachment download chat=%s url=%s -> HTTP %s (permanent)",
                            chat_id, url, e.code)
                return None
            reason: Any = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            _cleanup_tmp()
            reason = getattr(e, "reason", e)
        except Exception as e:
            # Over-size cap or a local write error — not a connectivity issue.
            _cleanup_tmp()
            log.warning("attachment download failed chat=%s url=%s: %s", chat_id, url, e)
            return None

        if attempt > ATTACHMENT_MAX_RETRIES:
            log.warning("attachment download giving up after %d attempt(s) chat=%s url=%s (%s)",
                        attempt, chat_id, url, reason)
            return None
        log.warning("attachment download transient (%s) — retry %d chat=%s url=%s",
                    reason, attempt, chat_id, url)
        time.sleep(delay)
        delay = min(delay * 2, YOUGILE_API_RETRY_MAX_DELAY)


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


async def _loop_heartbeat_task() -> None:
    """Prove the event loop is alive by bumping a monotonic timestamp."""
    global _loop_heartbeat
    while True:
        _loop_heartbeat = time.monotonic()
        await asyncio.sleep(5)


def _loop_watchdog() -> None:
    """Force-exit if the event loop stops heart-beating so launchd (KeepAlive)
    brings up a fresh process. This is the recovery path for a genuinely wedged
    loop — the very failure mode that once left us silently dead. Network
    outages are NOT a trigger (they're handled by retries + the subscription
    watcher); a restart wouldn't fix those anyway. The threshold is far larger
    than any normal loop pause, so a busy-but-healthy process is never killed.
    """
    while True:
        time.sleep(LOOP_WATCHDOG_CHECK_SECONDS)
        age = time.monotonic() - _loop_heartbeat
        if age > LOOP_WATCHDOG_TIMEOUT_SECONDS:
            log.critical(
                "watchdog: event loop stalled for %.0fs (> %ds) — exiting for a clean restart",
                age, LOOP_WATCHDOG_TIMEOUT_SECONDS,
            )
            os._exit(1)


_heartbeat_task_ref: Optional["asyncio.Task"] = None


@app.on_event("startup")
async def _start_background_tasks() -> None:
    # Arm the loop heartbeat + watchdog only once the loop is actually running,
    # so the blocking import-time startup work can't be mistaken for a stall.
    global _loop_heartbeat, _heartbeat_task_ref
    _loop_heartbeat = time.monotonic()
    # Keep a strong reference — asyncio only holds a weak one, so a fire-and-
    # forget task can be GC'd mid-await.
    _heartbeat_task_ref = asyncio.create_task(_loop_heartbeat_task())
    if LOOP_WATCHDOG_TIMEOUT_SECONDS > 0:
        threading.Thread(target=_loop_watchdog, daemon=True).start()
        log.info(
            "watchdog: armed (timeout=%ds, check=%ds)",
            LOOP_WATCHDOG_TIMEOUT_SECONDS, LOOP_WATCHDOG_CHECK_SECONDS,
        )


_active_lock = threading.Lock()
_active_procs: Set[int] = set()
# chat_id -> the live Popen for that chat's current turn. Lets us interrupt an
# in-flight turn when a new instruction arrives. Guarded by _active_lock.
_chat_procs: Dict[str, subprocess.Popen] = {}
# pids we deliberately killed to interrupt a turn — _reap must NOT mistake their
# non-zero exit for a transient API failure and retry them. Guarded by _active_lock.
_interrupted_pids: Set[int] = set()
# chat_id -> (rule, payload) queued to run the instant the current turn dies.
# Set when we interrupt a busy chat; consumed by _release_chat. Guarded by _active_lock.
_pending_turns: Dict[str, Tuple[Rule, Any]] = {}

_chat_locks_mutex = threading.Lock()
_chat_busy: Dict[str, threading.Lock] = {}


def _get_chat_lock(chat_id: str) -> threading.Lock:
    with _chat_locks_mutex:
        lock = _chat_busy.get(chat_id)
        if lock is None:
            lock = threading.Lock()
            _chat_busy[chat_id] = lock
        return lock


def _terminate_proc_group(pid: int) -> None:
    """SIGTERM the turn's process group, escalating to SIGKILL if it lingers.

    Spawned with start_new_session=True, so pid == pgid and a single killpg
    reaches claude plus every tool / MCP / subagent child it started. The grace
    period lets claude flush its session transcript so the --resume that follows
    still finds a usable session. Runs in its own thread (fire-and-forget) so the
    webhook handler never blocks on the kill.
    """
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return
    for _ in range(max(0, INTERRUPT_SIGKILL_GRACE_TICKS)):
        time.sleep(0.1)
        try:
            os.killpg(pid, 0)
        except (ProcessLookupError, OSError):
            return  # whole group is gone
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


def _interrupt_chat(chat_id: str) -> bool:
    """Kill the chat's in-flight turn so a fresh one can take over. Returns True
    if there was a live process to interrupt. Marks the pid so _reap skips the
    transient-error retry path for an intentional kill."""
    with _active_lock:
        proc = _chat_procs.get(chat_id)
        if proc is None:
            return False
        pid = proc.pid
        _interrupted_pids.add(pid)
    threading.Thread(target=_terminate_proc_group, args=(pid,), daemon=True).start()
    return True


def _post_capacity_message(chat_id: Optional[str]) -> None:
    """Tell the chat we're at the global concurrency cap (fire-and-forget)."""
    if not (chat_id and CONCURRENCY_BUSY_MESSAGE):
        return

    def _send() -> None:
        try:
            send_chat_message(chat_id, CONCURRENCY_BUSY_MESSAGE, CONCURRENCY_BUSY_MESSAGE_HTML)
        except Exception:
            log.exception("capacity message send failed chat=%s", chat_id)

    threading.Thread(target=_send, daemon=True).start()


def _release_chat(chat_id: Optional[str], chat_lock: Optional[threading.Lock]) -> None:
    """Release a chat's turn lock, then run any turn queued while it was busy.

    Single exit point for every turn (normal completion, spawn failure, or
    exception), so an interrupting instruction recorded in _pending_turns is
    never orphaned. The queued turn starts here — chained off the dying turn —
    which is why interrupting never blocks the webhook handler.
    """
    if chat_lock is not None:
        chat_lock.release()
    if not chat_id:
        return
    with _active_lock:
        pending = _pending_turns.pop(chat_id, None)
    if pending is None:
        return
    p_rule, p_payload = pending
    ok, why = spawn_claude(p_rule, p_payload, interrupted=True)
    if not ok and why == "global-max-concurrent":
        _post_capacity_message(chat_id)


def _build_prompt(
    rule: Rule,
    payload,
    chat_history_text: str,
    is_first_turn: bool,
    attachment_paths: List[Path],
    session_id: Optional[str] = None,
    interrupted: bool = False,
) -> str:
    event_json = json.dumps(payload, ensure_ascii=False, indent=2)
    rendered = rule.prompt_template
    rendered = rendered.replace("{event_json}", event_json)
    rendered = rendered.replace("{chat_history}", chat_history_text)
    rendered = rendered.replace("{first_turn}", "true" if is_first_turn else "false")
    rendered = rendered.replace("{formatting}", FORMATTING_BLOCK)
    rendered = rendered.replace("{language}", rule.language or "")
    rendered = rendered.replace("{session_id}", session_id or "")
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
    if interrupted and INTERRUPT_NOTICE:
        rendered = f"{INTERRUPT_NOTICE}\n\n---\n{rendered}"
    return rendered


def _spawn_subprocess(
    rule: Rule,
    cmd: List[str],
    header: str,
    on_exit,
    chat_id: Optional[str] = None,
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
        if chat_id:
            _chat_procs[chat_id] = proc
    log.info(
        "rule %s: claude spawned pid=%s attempt=%d cmd=%s",
        rule.name, pid, attempt, cmd[:3] + ["..."],
    )

    def _reap():
        rc = proc.wait()
        with _active_lock:
            _active_procs.discard(pid)
            was_interrupted = pid in _interrupted_pids
            _interrupted_pids.discard(pid)
            if chat_id and _chat_procs.get(chat_id) is proc:
                del _chat_procs[chat_id]
        log_fp.write(
            f"\n--- exit rc={rc} attempt={attempt} "
            f"ts={datetime.now(timezone.utc).isoformat()} ---\n"
        )
        log_fp.flush()
        end_pos = log_fp.tell()
        log_fp.close()
        log.info("rule %s: claude pid=%s attempt=%d rc=%s", rule.name, pid, attempt, rc)

        if (not was_interrupted and rc != 0 and attempt < CLAUDE_API_RETRIES
                and _output_is_transient(output_start, end_pos)):
            delay = CLAUDE_RETRY_DELAYS[min(attempt - 1, len(CLAUDE_RETRY_DELAYS) - 1)]
            log.warning(
                "rule %s: transient API error (rc=%s) — retrying in %ds (attempt %d/%d)",
                rule.name, rc, delay, attempt + 1, CLAUDE_API_RETRIES,
            )

            def _delayed_retry():
                time.sleep(delay)
                _spawn_subprocess(rule, cmd, header, on_exit, chat_id=chat_id, attempt=attempt + 1)

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


def spawn_claude(rule: Rule, payload, interrupted: bool = False) -> Tuple[bool, str]:
    chat_id = extract_chat_id(payload) if rule.session_per_chat else None
    chat_lock: Optional[threading.Lock] = _get_chat_lock(chat_id) if chat_id else None

    if chat_lock is not None and not chat_lock.acquire(blocking=False):
        # A turn is already running for this chat. Don't drop the new message —
        # queue it and interrupt the in-flight turn; the queued turn starts the
        # instant the current one dies (chained from _release_chat), resuming the
        # same session so the agent sees the new instruction and can redirect.
        with _active_lock:
            _pending_turns[chat_id] = (rule, payload)
        if _interrupt_chat(chat_id):
            log.info("rule %s: chat %s busy — interrupting current turn for new instruction",
                     rule.name, chat_id)
            return True, "interrupting"
        # No live process to kill — the holder is either finishing right now or
        # sitting in a retry backoff. Reclaim our queued turn and try to run it
        # directly so it can't dangle; if the lock is genuinely still held (retry
        # backoff), leave it queued for that holder's _release_chat to chain.
        with _active_lock:
            pending = _pending_turns.pop(chat_id, None)
        if pending is None:
            return True, "interrupting"  # a _release_chat already chained it
        if not chat_lock.acquire(blocking=False):
            with _active_lock:
                _pending_turns.setdefault(chat_id, pending)
            log.info("rule %s: chat %s busy (lock held) — queued pending turn", rule.name, chat_id)
            return True, "queued"
        rule, payload = pending
        interrupted = True  # treat the reclaimed turn as an interrupting one

    # No per-chat session, or the chat is idle and we now hold its lock. Enforce
    # the global concurrency cap for genuinely new turns — an interrupt replaces
    # an existing turn (1-for-1) and never reaches here.
    with _active_lock:
        if len(_active_procs) >= CLAUDE_MAX_CONCURRENT:
            if chat_lock is not None:
                chat_lock.release()
            return False, "global-max-concurrent"

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

        prompt = _build_prompt(
            rule, payload, history_text, is_first_turn, attachment_paths,
            session_id=session_id, interrupted=interrupted,
        )
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
                _release_chat(chat_id, chat_lock)

        proc = _spawn_subprocess(rule, cmd, header, on_exit, chat_id=chat_id)
        if proc is None:
            _release_chat(chat_id, chat_lock)
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
        _release_chat(chat_id, chat_lock)
        return False, "exception"


# ---------------------------------------------------------------- HTTP

@app.get("/healthz")
async def healthz():
    # Never hit the network here: a health check must return instantly even when
    # YouGile is unreachable, so it stays a reliable liveness signal. The webhook
    # subscription snapshot is whatever the background watcher last observed.
    with _webhook_status_lock:
        webhook = {
            "subscriptions": list(_webhook_status["subscriptions"]),
            "healthy": _webhook_status["healthy"],
            "checked_at": _webhook_status["checked_at"],
            "last_ensure_ok": _webhook_status["last_ensure_ok"],
        }
    return {
        "ok": True,
        "loop_heartbeat_age_s": round(time.monotonic() - _loop_heartbeat, 1),
        "claude_bin": CLAUDE_BIN,
        "default_workdir": CLAUDE_DEFAULT_WORKDIR,
        "active_claude_procs": len(_active_procs),
        "tracked_chats": list_known_chats(),
        "webhook": webhook,
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
    if rule and is_duplicate_delivery(payload):
        # YouGile re-sent a message we already dispatched. Re-running it would
        # interrupt the in-flight turn — fatally so while that turn is still
        # creating the session. The first delivery already reacted and spawned,
        # so this one is a pure no-op.
        spawn_reason = "duplicate"
    elif rule:
        # Fast "received" signal: react to the trigger message the instant it
        # matches a rule — before spawning the agent and before any ack reply.
        # Fire-and-forget in a thread so the extra API round-trip never delays
        # the webhook response. No-ops for non-chat events (no numeric message
        # id) and when ACK_REACTION_EMOJI is empty.
        if ACK_REACTION_EMOJI:
            react_chat_id = extract_chat_id(payload)
            react_msg_id = extract_message_id(payload)
            if react_chat_id and react_msg_id:
                threading.Thread(
                    target=react_to_message,
                    args=(react_chat_id, react_msg_id, ACK_REACTION_EMOJI),
                    daemon=True,
                ).start()
        # spawn_claude reads chat history synchronously (now with infinite API
        # retries), so run it off the event loop — a VPN flap mid-fetch must not
        # stall healthz or the acceptance of other webhook deliveries.
        ok, why = await asyncio.to_thread(spawn_claude, rule, payload)
        spawn_reason = why
        if ok:
            fired_rule = rule.name
        elif why == "global-max-concurrent":
            # Saturated by OTHER chats — tell the user instead of dropping silently.
            _post_capacity_message(extract_chat_id(payload))

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
