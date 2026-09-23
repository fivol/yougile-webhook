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
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml
from fastapi import FastAPI, Request

# Hand-maintained; bump on every change worth restarting the service for. The
# launchd agent runs uvicorn from the checkout, so /healthz reporting this is
# the quickest way to tell whether a restart actually picked up new code.
APP_VERSION = "1.7.0"

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
log.info("yougile-webhook %s starting from %s", APP_VERSION, ROOT)


# --- Settings ----------------------------------------------------------------
# Seventy flat environment variables is a lot of surface for something whose
# knobs group naturally (rotation, handoff, history, turns). config.yaml gives
# them that shape; config.local.yaml overrides it per machine and is not
# committed. Environment variables still win over both, so a one-off override
# needs no file edit and every existing .env keeps working unchanged.
#
# Secrets stay in .env deliberately: config.yaml is meant to be committable.

CONFIG_FILE = ROOT / os.environ.get("CONFIG_FILE", "config.yaml")
CONFIG_LOCAL_FILE = ROOT / os.environ.get("CONFIG_LOCAL_FILE", "config.local.yaml")


class _StrictBoolLoader(yaml.SafeLoader):
    """YAML 1.1 reads `no`, `off` and `yes` as booleans.

    That is a live hazard here: `language: no` is Norwegian, not False, and the
    same goes for locale lists and two-letter codes generally. Only `true` and
    `false` stay boolean; everything else that looks like a word stays a string.
    """


_StrictBoolLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)
for _ch in list("yYnNoO"):
    _StrictBoolLoader.yaml_implicit_resolvers.setdefault(_ch, [])
    _StrictBoolLoader.yaml_implicit_resolvers[_ch] = [
        (tag, rx) for tag, rx in _StrictBoolLoader.yaml_implicit_resolvers[_ch]
        if tag != "tag:yaml.org,2002:bool"
    ]


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_config() -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for path in (CONFIG_FILE, CONFIG_LOCAL_FILE):
        if not path.exists():
            continue
        try:
            with path.open("rb") as f:
                data = yaml.load(f, Loader=_StrictBoolLoader) or {}
        except (OSError, yaml.YAMLError):
            log.exception("config %s is unreadable — ignoring it", path)
            continue
        if not isinstance(data, dict):
            log.warning("config %s is not a mapping — ignoring it", path)
            continue
        merged = _deep_merge(merged, data)
        log.info("config: loaded %s", path.name)
    return merged


CONFIG: Dict[str, Any] = _load_config()


def conf(path: str, env: str, default: Any, cast: Any = str) -> Any:
    """One setting, resolved: environment > config.local.yaml > config.yaml > default.

    `path` is dotted ("rotation.turn_cost_usd"); `env` is the variable that has
    always set it. Keeping both means no existing deployment has to change and
    nothing has to be looked up in two places to know what is in effect.
    """
    raw = os.environ.get(env)
    if raw is None:
        node: Any = CONFIG
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        raw = node
    if raw is None:
        return default
    try:
        if cast is bool:
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() not in ("0", "false", "no", "off", "")
        if cast is list:
            if isinstance(raw, list):
                return [str(x).strip() for x in raw if str(x).strip()]
            return [x.strip() for x in str(raw).split(",") if x.strip()]
        return cast(raw)
    except (TypeError, ValueError):
        log.warning("config %s: %r is not a valid %s — using %r",
                    path, raw, getattr(cast, "__name__", cast), default)
        return default


# --- User-facing text --------------------------------------------------------
# Everything this receiver posts into a chat lives in messages/<lang>.toml, not
# in the code: the code is English, the chats it serves are not, and a file of
# strings is something you can hand to someone who doesn't read Python. Loaded
# once at import — the strings are needed by module-level constants, and a
# reload of the service is how every other config change lands anyway.
MESSAGES_LANG = os.environ.get("MESSAGES_LANG", "ru").strip() or "ru"
MESSAGES_FILE = os.environ.get("MESSAGES_FILE", "").strip()
MESSAGES_DIR = ROOT / "messages"


def _flatten_messages(table: Dict[str, Any], prefix: str = "") -> Dict[str, str]:
    """TOML tables to "table.key" paths, which is how the code addresses them."""
    flat: Dict[str, str] = {}
    for key, value in table.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten_messages(value, f"{path}."))
        else:
            flat[path] = str(value)
    return flat


def load_messages() -> Dict[str, str]:
    path = Path(MESSAGES_FILE) if MESSAGES_FILE else MESSAGES_DIR / f"{MESSAGES_LANG}.toml"
    try:
        with path.open("rb") as f:
            flat = _flatten_messages(tomllib.load(f))
    except (OSError, tomllib.TOMLDecodeError):
        # Never fatal. A chat that answers with bare keys is bad; a receiver
        # that refuses to start — and so drops every mention — is worse.
        log.exception("messages file %s unusable — falling back to bare keys", path)
        return {}
    log.info("messages: %d strings from %s", len(flat), path)
    return flat


MESSAGES = load_messages()


def msg(key: str, **kwargs: Any) -> str:
    """Render a user-facing string by "table.key".

    Degrades instead of raising: a missing key or a template someone broke while
    translating must never take down the turn that was trying to report
    something — least of all a failure report.
    """
    template = MESSAGES.get(key)
    if template is None:
        log.error("message %r missing from the %s messages file", key, MESSAGES_LANG)
        return key
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        log.exception("message %r failed to render", key)
        return template


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
# Agents start their repos' dev stacks (celery, gunicorn, ...) and those inherit
# our environment. On macOS a forked worker opening a Postgres connection makes
# libpq probe the Kerberos ccache for GSS encryption; that goes through XPC,
# which is not fork-safe, so every such worker segfaults and pops a crash
# dialog. Nobody here talks GSS to Postgres — skip the probe unless overridden.
os.environ.setdefault("PGGSSENCMODE", "disable")
CLAUDE_DEFAULT_WORKDIR = os.environ.get("CLAUDE_WORKDIR") or str(ROOT)
CLAUDE_DEFAULT_EXTRA_ARGS = shlex.split(
    os.environ.get("CLAUDE_EXTRA_ARGS", "--dangerously-skip-permissions")
)
CLAUDE_MAX_CONCURRENT = conf(
    "claude.max_concurrent", "CLAUDE_MAX_CONCURRENT", 10, int)

# Reasoning effort every spawned turn runs at. Without the flag the CLI falls
# back to whatever `effortLevel` the machine's settings.json happens to carry —
# invisible from here, and different on the next machine. Naming it makes what
# the service runs a fact of this repo. Set it empty to hand the choice back to
# settings.json; a rule that puts its own --effort in extra_args still wins.
CLAUDE_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
CLAUDE_EFFORT = conf("claude.effort", "CLAUDE_EFFORT", "xhigh", str).strip().lower()
if CLAUDE_EFFORT and CLAUDE_EFFORT not in CLAUDE_EFFORT_LEVELS:
    # A typo here would make the CLI reject every single spawn, so drop the
    # flag instead of shipping a broken command line.
    log.warning("config claude.effort: %r is not one of %s — not passing --effort",
                CLAUDE_EFFORT, ", ".join(CLAUDE_EFFORT_LEVELS))
    CLAUDE_EFFORT = ""


def effort_args(extra_args: List[str]) -> List[str]:
    """`--effort <level>`, unless disabled or the rule already names one."""
    if not CLAUDE_EFFORT:
        return []
    if any(a == "--effort" or a.startswith("--effort=") for a in extra_args):
        return []
    return ["--effort", CLAUDE_EFFORT]


# The agent reaches YouGile through mcp_yougile.py, in this repo, handed to
# claude explicitly rather than registered by hand in ~/.claude.json. Two
# reasons, in order of importance:
#
#   * The reply channel is ours. The upstream server's schema described
#     `textHtml` as "optional, rarely needed", and a schema read at call time
#     beats a system prompt: about half of all replies went out as unformatted
#     markdown. Our server repairs the message on the way out instead.
#   * A deployment is reproducible from the checkout. Server, schema and tool
#     set come from this working tree; the machine only supplies the API key,
#     in .env with every other secret.
#
# Written on startup rather than committed because the paths are machine-local
# (this checkout, this interpreter). Deliberately NOT --strict-mcp-config: the
# agent's turns lean on the user's other MCP servers — chrome-devtools carries
# more calls than every YouGile tool combined, and figma is used for design
# work — so the user's config stays in play and ours is merged over it. A
# same-named server in --mcp-config wins, which is what keeps `yougile-mcp`
# tool names stable for sessions resumed from before this file existed.
MCP_CONFIG_FILE = STATE_DIR / "mcp.json"
MCP_SERVER_SCRIPT = ROOT / "mcp_yougile.py"


def write_mcp_config() -> bool:
    """Refresh state/mcp.json. False if the agent must run without our server."""
    if not MCP_SERVER_SCRIPT.is_file():
        log.error("MCP: %s is missing — falling back to whatever ~/.claude.json "
                  "registers, where an unformatted reply is not repaired",
                  MCP_SERVER_SCRIPT)
        return False
    config = {
        "mcpServers": {
            "yougile-mcp": {
                "type": "stdio",
                # This interpreter: the server imports chatmd from the same tree
                # and must not depend on what `python3` means in the agent's cwd.
                "command": sys.executable,
                "args": [str(MCP_SERVER_SCRIPT)],
                # The API key is inherited from this process (it is in .env and
                # the server re-reads it anyway) and stays out of the file.
                "env": {"PLAIN_REPLY_MIN_CHARS": str(PLAIN_REPLY_MIN_CHARS)},
            }
        }
    }
    try:
        MCP_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        MCP_CONFIG_FILE.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except OSError:
        log.exception("MCP: cannot write %s", MCP_CONFIG_FILE)
        return False


def mcp_args() -> List[str]:
    return ["--mcp-config", str(MCP_CONFIG_FILE)] if MCP_CONFIG_FILE.is_file() else []

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

# Prepended to the reply contract when the agent's previous reply in this chat
# went out unformatted. The agent never sees its own messages rendered — it gets
# no feedback from the channel at all — so a turn that dropped `textHtml` has no
# way of knowing, and the next turn repeats it. This is that feedback.
PLAINTEXT_NOTICE = (
    "⚠️ Your previous reply in this chat was posted as PLAIN TEXT — it went "
    "out with `textHtml` empty, so the user got an unformatted wall instead of "
    "the report you wrote. Do not repeat it in this turn."
)

# Same feedback loop, louder failure: `textHtml` arrived wrapped in an XML CDATA
# section. YouGile stores the string verbatim and its renderer reads
# `<![CDATA[ ... ]]>` as one non-element node, so the message lands as an EMPTY
# bubble — not unformatted, absent. Rare (4 of 858 formatted replies since May)
# and self-repeating, because nothing in the channel tells the agent. The MCP
# server strips the wrapper before the message is posted (chatmd.strip_cdata);
# this is the net under it, for a reply that reaches the chat anyway — which now
# means our server did not answer the call, so read state/mcp.json first.
CDATA_NOTICE = (
    "⚠️ Your previous reply in this chat had its `textHtml` wrapped in "
    "`<![CDATA[ ... ]]>`. YouGile renders that as an EMPTY message — the user "
    "saw a blank bubble where your report should have been. Send the HTML on "
    "its own, with no CDATA wrapper, in this turn."
)

# Retry policy for transient API errors. The Anthropic API surfaces overload /
# rate-limit failures as 5xx; the claude CLI prints them to stdout and exits
# with a non-zero code. We re-spawn the same command after a backoff.
CLAUDE_API_RETRIES = conf("claude.api_retries", "CLAUDE_API_RETRIES", 3, int)
CLAUDE_RETRY_DELAYS = [
    int(x) for x in os.environ.get("CLAUDE_RETRY_DELAYS", "30,120,300").split(",")
    if x.strip()
] or [30, 120, 300]
CLAUDE_RETRY_PATTERN = re.compile(
    r"API Error:\s*5\d\d|overloaded_error|rate_limit_error|\bOverloaded\b",
    re.IGNORECASE,
)

# --- A turn must never die silently ------------------------------------------
# A turn can die before the agent gets a single word out: a --resume that finds
# no session, a bad flag, an expired login, a spawn that never happens. The
# trigger message has been 👍'd by then, so the chat shows "received" and then
# nothing forever — from the outside, a dead turn is indistinguishable from a
# slow one. Every failed turn now posts what actually went wrong.
TURN_FAILURE_REPORT = conf("turn.report_failure", "TURN_FAILURE_REPORT", True, bool)
TURN_FAILURE_PREFIX = os.environ.get("TURN_FAILURE_PREFIX", "⚠️").strip()
# How much of claude's own output to quote back into the chat.
TURN_FAILURE_QUOTE_CHARS = int(os.environ.get("TURN_FAILURE_QUOTE_CHARS", "600"))
# Bound on how much of a turn's output the failure report reads back. Startup
# errors are the first thing printed and the answer is the last, so keeping the
# head and the tail of the stream covers every case the report cares about.
CLAUDE_CAPTURE_HEAD_CHARS = 8 * 1024
CLAUDE_CAPTURE_TAIL_CHARS = 56 * 1024

# --- A turn outlives the process that started it ------------------------------
# claude runs in its own process session (start_new_session=True) so a webhook
# restart cannot kill a running turn. Everything the receiver needs to finish
# that turn therefore has to be on disk too, or a restart orphans it: the
# watermark never advances, the card keeps saying "working", the queued turn
# never starts, and the chat is told nothing. One file per chat, written the
# moment the process is live, removed when the turn is finalised.
TURNS_STATE_DIR = STATE_DIR / "turns"
TURNS_STATE_DIR.mkdir(parents=True, exist_ok=True)
# claude's stdout goes to a file rather than a pipe. A pipe has a reader that
# dies with the receiver — after which claude either blocks on a full buffer or
# is killed writing to a closed one, mid-turn, having answered nothing. A file
# has no reader to lose, so an orphaned turn runs to completion and still posts
# its own reply.
TURN_LOG_DIR = LOG_DIR / "turns"
TURN_LOG_DIR.mkdir(parents=True, exist_ok=True)
# Keep the last N turn logs; older ones are reaped as new turns start.
TURN_LOG_KEEP = conf("turn.log_keep", "TURN_LOG_KEEP", 200, int)
# A turn that exits cleanly but posts nothing looks identical to a working one
# from the chat: the 👍 is there and no reply ever comes. Say so instead.
TURN_REPORT_SILENT = conf("turn.report_silent", "TURN_REPORT_SILENT", True, bool)
# How often the re-attached supervisor polls an adopted (no longer child)
# process to notice it has exited.
TURN_ADOPT_POLL_SECONDS = conf(
    "turn.adopt_poll_seconds", "TURN_ADOPT_POLL_SECONDS", 5.0, float)

# `claude --resume <id>` on a session that is no longer on disk exits 1 within
# seconds having printed just this one line. That id is then unusable in BOTH
# directions (--resume can't find it, --session-id can consider it taken), so
# the only way out is a fresh link in the chat's session chain — otherwise the
# chat is bricked and every later mention dies the same way.
# Matching here only means "the prompt never reached a session". Whether the
# session is actually GONE is a second question finish_turn answers by looking
# for the transcript: "already in use" over a transcript that is right there is
# a collision, and rotating away from it would discard a live conversation.
SESSION_LOST_PATTERN = re.compile(
    r"No conversation found with session ID"
    r"|Session ID .{0,80}? is already in use"
    r"|Invalid session ID",
    re.IGNORECASE,
)
# Other ways a turn dies with one line and no work done. Matching them by name
# means the chat gets the actual cause instead of the tail of whatever scrolled
# past last — these are the ones the user can act on themselves.
CLAUDE_FATAL_PATTERN = re.compile(
    r"usage limit reached"
    r"|credit balance is too low"
    r"|Invalid API key"
    r"|OAuth token has expired"
    r"|Please run /login"
    r"|command not found"
    r"|No such file or directory"
    r"|error: unknown option"
    r"|Permission denied",
    re.IGNORECASE,
)
# Give _release_chat time to start whatever was queued behind the dead turn
# before the replay checks whether the chat is busy.
SESSION_RECOVERY_DELAY_SECONDS = float(
    os.environ.get("SESSION_RECOVERY_DELAY_SECONDS", "2"))
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

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

CHAT_HISTORY_FIRST_TIME_LIMIT = conf(
    "history.first_time_limit", "CHAT_HISTORY_FIRST_TIME_LIMIT", 20, int)
CHAT_HISTORY_DELTA_LIMIT = conf(
    "history.delta_limit", "CHAT_HISTORY_DELTA_LIMIT", 50, int)

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

# --- The agent's status sticker ----------------------------------------------
# A sticker on the task card answers the one question a human glancing at the
# board has: is the agent still working on this, or is something waiting for me
# to read?
#
#   "working"  -> a turn is running, the answer isn't in yet — wait
#   "answered" -> the agent stopped and left its reply in the chat — go read it
#
# A turn sets `working` the moment it spawns and `answered` when the process
# exits. `answered` deliberately does NOT mean "the task is done" — a turn that
# merely answered a question sets it too. The signal is "your turn, human".
#
# Why a sticker and not the task's `completed` checkbox: YouGile refuses a
# `completed` write (in BOTH directions, HTTP 400 "Не удалось изменить задачу")
# from any account that isn't among the task's assignees, so the checkbox can't
# carry the agent's status on a task assigned to a human. A string sticker is
# writable on any task, shows on the card, and collides with nothing.
AGENT_STATUS_STICKER = os.environ.get("AGENT_STATUS_STICKER", "true").lower() not in ("0", "false", "no")
# Looked up by name and created on first use if missing. Enable it on the boards
# you want it visible on (one-time, in the YouGile UI). State names must match
# exactly — YouGile's API can rename a sticker but cannot edit its states, so a
# typo here means adding the state by hand in the UI.
AGENT_STICKER_NAME = os.environ.get("AGENT_STICKER_NAME", "Agent").strip()
AGENT_STICKER_ICON = os.environ.get("AGENT_STICKER_ICON", "pin").strip()
AGENT_STICKER_WORKING = os.environ.get("AGENT_STICKER_WORKING", "working").strip()
AGENT_STICKER_ANSWERED = os.environ.get("AGENT_STICKER_ANSWERED", "answered").strip()
AGENT_STICKER_WORKING_COLOR = os.environ.get("AGENT_STICKER_WORKING_COLOR", "#8CC2FF").strip()
AGENT_STICKER_ANSWERED_COLOR = os.environ.get("AGENT_STICKER_ANSWERED_COLOR", "#79CE69").strip()

# --- Session rotation --------------------------------------------------------
# A chat's claude session grows without bound: every turn re-sends the whole
# conversation. Two costs follow from that, and both scale linearly with the
# context size:
#
#   cache HIT  -> the prefix is served from cache at 0.1x the input price
#   cache MISS -> the whole prefix is re-written at 2x (1h TTL) — a 20x jump
#
# Whether the next turn hits is almost entirely a function of how long the chat
# has been idle (measured over 88 real turns on this machine):
#
#   <10 min  7% miss     30-60 min  75% miss
#   10-30    24% miss    >60 min   100% miss
#
# The miss is the smaller half of the story, though. ONE agent turn is not one
# API call — the agent reads, edits, greps, screenshots, and every one of those
# steps re-sends the whole prefix again. Three consecutive turns on one real
# task measured 106, 64 and 22 calls; a turn resumed on a 313k context was
# paying ~31k tokens per call before it did anything. So the read side gets
# multiplied by the length of the turn and the write side does not:
#
#   expected input = ctx * (calls * READ_MULT + p_miss * (WRITE_MULT - READ_MULT))
#
# Both inputs are knowable BEFORE spending a token: `ctx_tokens` comes from the
# previous turn's usage block, the idle time from the clock, and `calls` from
# the running average of how long this machine's turns actually are. When that
# expected cost crosses ROTATE_TURN_COST_USD we retire the session and start a
# fresh one, carrying the essence over in a handoff file (task history +
# workspace + PR diffs) instead of the raw transcript. A short exchange keeps
# its context; a big one resets, because on a fat context every single tool call
# is billed against the whole transcript.
SESSION_ROTATE_MODE = conf(
    "rotation.mode", "SESSION_ROTATE_MODE", "manual", str).strip().lower()
# The one knob that matters: rotate once the next turn's expected input cost
# exceeds this. In dollars, so it means something without a token calculator.
# At the default turn length this puts the rotate point around 130k of context
# while warm and around 100k after an hour's pause — comfortably above what a
# handoff costs to hand over (~7k, plus ~5 calls of re-orientation), so a
# rotation always buys back more than it spends.
ROTATE_TURN_COST_USD = conf(
    "rotation.turn_cost_usd", "ROTATE_TURN_COST_USD", 3.00, float)
# Rotate regardless of cost past this context size — the 1M window is a wall we
# must not hit mid-task (observed sessions reached 950k).
ROTATE_HARD_CTX = conf("rotation.hard_ctx", "ROTATE_HARD_CTX", 600000, int)
# Never rotate a small session, whatever the arithmetic says. Below this the
# handoff plus re-orientation costs more than staying put.
ROTATE_MIN_CTX = conf("rotation.min_ctx", "ROTATE_MIN_CTX", 60000, int)
# Seed for "how many API calls does one turn make". Self-corrects from observed
# turns; this value only matters until the first one lands.
ROTATE_CALLS_PER_TURN = conf(
    "rotation.calls_per_turn", "ROTATE_CALLS_PER_TURN", 45.0, float)

# --- Handoff -----------------------------------------------------------------
# What the new session gets instead of the old transcript. Everything here is
# assembled mechanically (L0) at zero model cost; only the optional digest (L1)
# spends anything, and it runs in a fresh cheap session over the L0 file rather
# than resuming the expensive one we are escaping.
HANDOFF_DIR = STATE_DIR / "handoff"
HANDOFF_HISTORY_LIMIT = conf(
    "handoff.history_limit", "HANDOFF_HISTORY_LIMIT", 0, int)  # 0 = all
HANDOFF_INLINE_MESSAGES = conf(
    "handoff.inline_messages", "HANDOFF_INLINE_MESSAGES", 30, int)
HANDOFF_DIGEST = conf(
    "handoff.digest.mode", "HANDOFF_DIGEST", "auto", str).strip().lower()
# A mechanical handoff runs ~25 KB (≈7k tokens) for a typical task, which is
# cheap to hand over as-is. Only a genuinely long chat is worth paying a model
# to compress.
HANDOFF_DIGEST_THRESHOLD_KB = conf(
    "handoff.digest.threshold_kb", "HANDOFF_DIGEST_THRESHOLD_KB", 64, int)
HANDOFF_DIGEST_MODEL = conf(
    "handoff.digest.model", "HANDOFF_DIGEST_MODEL", "sonnet", str).strip()
HANDOFF_DIGEST_TIMEOUT = int(os.environ.get("HANDOFF_DIGEST_TIMEOUT", "180"))
HANDOFF_SKELETON_MAX_CHARS = int(os.environ.get("HANDOFF_SKELETON_MAX_CHARS", "200000"))
# How many of the retired session's own remarks to carry over. They are the
# priciest part of the handoff and the least load-bearing — running commentary
# between tool calls, whose conclusions are already in the chat. A few keep the
# "where was it cut off" signal; 0 drops the section entirely.
HANDOFF_NARRATION = conf("handoff.narration", "HANDOFF_NARRATION", 6, int)
# Wrapped around the handoff at the top of a rotated session's first prompt.
# The content is inlined rather than pointed at: a Read tool-result lands in the
# same context and is cached the same way, so the file bought nothing but an
# extra round-trip — and, because the prompt also carried {chat_history}, the
# whole chat was being delivered twice.
HANDOFF_BLOCK = """\
SESSION HANDOFF — the previous claude session for this task was retired because
its context grew too expensive to carry. You are continuing the SAME task in a
fresh session, so you start with no memory of it. Everything known about the
task is below; it was assembled mechanically, so read it before acting.

After reading it, and before answering:

1. For every repo listed below:
   `git -C <worktree>/<repo> diff origin/<base>...HEAD --stat`, then the full
   diff of the files that matter for the current request.
2. If the task has open PRs, check the review comments:
   `gh pr view <url> --comments`.
3. Only then act on the latest message in the chat.

Do NOT create a new branch, worktree or PR — the existing ones are named below
and the `feature` skill will resume them. If you need a detail this doesn't
cover, grep the old transcript (its path is at the bottom) rather than guessing.

════════════════════════ HANDOFF ════════════════════════
{body}
═════════════════════ END OF HANDOFF ════════════════════\
"""

# --- Pricing -----------------------------------------------------------------
# Anthropic API list price. Every dollar figure the webhook reports is an
# estimate at these rates — on a subscription the absolute numbers are notional
# but the ratios (and therefore the rotation decision) still hold.
PRICE_IN_PER_MTOK = float(os.environ.get("PRICE_IN_PER_MTOK", "5.0"))
PRICE_OUT_PER_MTOK = float(os.environ.get("PRICE_OUT_PER_MTOK", "25.0"))
CACHE_WRITE_MULT = float(os.environ.get("CACHE_WRITE_MULT", "2.0"))  # 1h TTL
CACHE_READ_MULT = float(os.environ.get("CACHE_READ_MULT", "0.1"))
CACHE_STATS_FILE = STATE_DIR / "cache_stats.json"

# --- Chat commands -----------------------------------------------------------
# Handled entirely by the receiver: no agent spawn, no tokens, no sticker
# change. `/status` is the important one — it answers "how big has this got and
# will the next turn start a new session?" without costing anything to ask.
COMMANDS_ENABLED = os.environ.get("COMMANDS_ENABLED", "true").lower() not in ("0", "false", "no")
COMMAND_ALLOWED_EMAILS = [
    e.strip().lower()
    for e in os.environ.get("COMMAND_ALLOWED_EMAILS", "").split(",")
    if e.strip()
]
# Marks our own command replies so they can be stripped back out of the history
# the next agent turn sees.
COMMAND_REPLY_PREFIX = os.environ.get("COMMAND_REPLY_PREFIX", "⚙️").strip()
# Append the cost/context block to the ack the agent posts ("Продолжаю …").
ACK_STATS = conf("chat.ack_stats", "ACK_STATS", True, bool)

# --- Replying to the agent counts as mentioning it ---------------------------
# Replying to (or forwarding) one of the agent's own messages is how a user says
# "you — keep going" without retyping the mention. YouGile records it as
# properties.replyTo = <message id> and leaves the text mention-free, so such a
# message would otherwise match no rule. When one arrives we look up the message
# it answers and, if the agent wrote it, inject AGENT_MENTION into the payload —
# the ordinary `@Agent` rules then fire on it unchanged.
REPLY_TO_AGENT_IS_MENTION = os.environ.get(
    "REPLY_TO_AGENT_IS_MENTION", "true"
).lower() not in ("0", "false", "no")
AGENT_MENTION = os.environ.get("AGENT_MENTION", "@Agent").strip()
# The agent's own YouGile account, needed to tell "reply to the agent" from
# "reply to a colleague". Normally learned by itself from the mention chunks
# YouGile embeds in "@Agent ..." messages (cached in state/agent.json); set
# either variable to pin it explicitly instead.
AGENT_USER_ID = os.environ.get("AGENT_USER_ID", "").strip()
AGENT_USER_EMAIL = os.environ.get("AGENT_USER_EMAIL", "").strip().lower()
# How much of the replied-to message to quote back into the prompt so the agent
# knows which of its own messages the user is answering.
REPLY_QUOTE_MAX_CHARS = int(os.environ.get("REPLY_QUOTE_MAX_CHARS", "2000"))
# Below this an agent message is an ack or a one-liner, where plain text is the
# right answer — only longer replies are expected to carry `textHtml`.
PLAIN_REPLY_MIN_CHARS = conf(
    "chat.plain_reply_min_chars", "PLAIN_REPLY_MIN_CHARS", 400, int)

RULES_FILE = ROOT / os.environ.get("RULES_FILE", "rules.toml")

# Engine text ships in the repo; an install customises it by dropping a file of
# the same name into `prompts/local/`, which shadows it. One idiom for every
# prompt, instead of `X.example.txt`-copy-me (two files that drift apart the
# first time either is edited) plus a `_`-prefixed name meaning "shared, don't
# copy". `local/` is gitignored wholesale, so what is committed is the engine
# and what is ignored is the instance — you can tell which is which by where the
# file lives, not by its name.
PROMPT_LOCAL_DIR = "local"


def resolve_prompt(path: Path) -> Path:
    """`prompts/local/<name>` if it exists, else the shipped `prompts/<name>`."""
    local = path.parent / PROMPT_LOCAL_DIR / path.name
    return local if local.is_file() else path

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
#
# The file is split by a marker line into two parts with different lifetimes:
# the spec above it goes in once per session (at {formatting}, first turn), the
# short contract below it is appended LAST to EVERY prompt. Presence is not the
# same as attention: a rule read on turn one, 100k tokens before the reply it
# governs, loses to whatever the agent read last — which, on an implement turn,
# is a skill's own output template. The part whose loss the agent cannot see and
# the user cannot miss (a plain-text wall instead of a report) is the part that
# has to be re-stated at the end of every turn. No marker => the whole file is
# the spec, exactly as before.
_FORMATTING_FILE = resolve_prompt(
    ROOT / os.environ.get("FORMATTING_FILE", "prompts/formatting.txt"))
_FORMATTING_MARKER = "=== ALWAYS ==="
_formatting_raw = (
    _FORMATTING_FILE.read_text(encoding="utf-8")
    if _FORMATTING_FILE.exists()
    else ""
)
FORMATTING_BLOCK, _, REPLY_CONTRACT = _formatting_raw.partition(_FORMATTING_MARKER)
FORMATTING_BLOCK = FORMATTING_BLOCK.strip()
REPLY_CONTRACT = REPLY_CONTRACT.strip()

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
    # Used from the second turn of a session onward, when the standing
    # instructions are already in the agent's context. Empty = always full.
    prompt_template_followup: str
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
        prompt_path = resolve_prompt(ROOT / raw.get("prompt_file", ""))
        if not prompt_path.exists():
            log.warning("rule %s: prompt file %s missing — skipping", name, prompt_path)
            continue
        # Optional. Without it every turn re-sends the full template, which a
        # resumed session already has — see _build_prompt. Defaults to
        # "<prompt>.followup.txt" beside the main one, so dropping the file in
        # is enough; no rules.toml change needed.
        followup = ""
        followup_file = raw.get("prompt_file_followup")
        followup_path = resolve_prompt(
            ROOT / followup_file if followup_file
            # Beside the *configured* prompt, not the resolved one: a local
            # override of the main template must not drag the shipped follow-up
            # out of `local/`, nor the other way round.
            else (ROOT / raw.get("prompt_file", "")).with_suffix(".followup.txt")
        )
        if followup_path.exists():
            followup = followup_path.read_text(encoding="utf-8")
        elif followup_file:
            log.warning(
                "rule %s: follow-up prompt %s missing — every turn will "
                "re-send the full template", name, followup_path,
            )
        rules.append(
            Rule(
                name=name,
                enabled=bool(raw.get("enabled", True)),
                events=set(raw.get("events", [])),
                pattern=re.compile(raw["pattern"]) if raw.get("pattern") else None,
                allowed_sender_emails=list(raw.get("allowed_sender_emails", [])),
                column_names=list(raw.get("column_names", [])),
                prompt_template=prompt_path.read_text(encoding="utf-8"),
                prompt_template_followup=followup,
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


def _canonical_webhook(subs: List[dict]) -> Optional[dict]:
    """Among several subscriptions for our URL, pick the single one to keep:
    prefer an enabled row, then the most recently successful, then the lowest
    failure streak. Deterministic, so repeated reconciles agree on the survivor.
    """
    if not subs:
        return None
    return max(
        subs,
        key=lambda s: (
            0 if s.get("disabled") else 1,
            s.get("lastSuccess") or 0,
            -(s.get("failuresSinceLastSuccess") or 0),
        ),
    )


def ensure_webhook_subscription(*, retries: Optional[int] = None) -> bool:
    """Reconcile YouGile to EXACTLY ONE enabled subscription for our URL.

    Run at startup and on a timer (WEBHOOK_ENSURE_INTERVAL_SECONDS). Two things
    can leave stale rows: YouGile disables a subscription after a streak of
    failed deliveries (it stays dead until we touch it), and a delete+recreate
    or a stray `create` can leave several rows for the same URL — and two LIVE
    rows mean YouGile delivers every event twice.

    Each call therefore keeps one canonical row, deletes every other (dead OR
    duplicate-live), and makes sure the survivor is enabled — recreating only
    when there is genuinely none. A duplicate live subscription is thus
    self-correcting: it survives at most one cycle before the next ensure prunes
    it. Returns True if a single healthy subscription is in place after the call.
    """
    if not (WEBHOOK_URL and YOUGILE_API_KEY):
        return False
    budget = YOUGILE_API_BOOT_RETRIES if retries is None else retries

    def _done(ok: bool) -> bool:
        with _webhook_status_lock:
            _webhook_status["last_ensure_ok"] = ok
        return ok

    def _prune(subs: List[dict]) -> None:
        for s in subs:
            sub_id = s.get("id")
            if sub_id:
                _api_put(f"/webhooks/{sub_id}", {"deleted": True}, retries=budget)
                log.info("ensure_webhook: pruned extra subscription %s (disabled=%s)",
                         sub_id, s.get("disabled"))

    ours = _list_our_webhooks(retries=budget)
    keep = _canonical_webhook(ours)
    if keep is not None:
        keep_id = keep.get("id")
        # Collapse every other row for this URL — dead ones and live duplicates.
        _prune([s for s in ours if s.get("id") != keep_id])
        if keep.get("disabled"):
            _api_put(f"/webhooks/{keep_id}", {"disabled": False}, retries=budget)
            log.info("ensure_webhook: re-enabled subscription %s for %s", keep_id, WEBHOOK_URL)
        # Confirm the survivor is live; defensively prune anything left over
        # (e.g. a delete that was still settling, or a racing live row).
        ours = _list_our_webhooks(retries=budget)
        live = [s for s in ours if not s.get("disabled")]
        if live:
            if len(ours) > 1:
                keep2_id = (_canonical_webhook(live) or {}).get("id")
                _prune([s for s in ours if s.get("id") != keep2_id])
            return _done(True)
        # The survivor refused to enable — drop everything and recreate below.
        _prune(ours)

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


# ------------------------------------------------------- agent status sticker

# YouGile's "this sticker has no value" sentinel.
STICKER_CLEAR = "-"
# The two states we drive, and the labels/colours to create them with.
MARKER_WORKING = "working"
MARKER_ANSWERED = "answered"
_MARKER_SPECS = {
    MARKER_WORKING: (AGENT_STICKER_WORKING, AGENT_STICKER_WORKING_COLOR),
    MARKER_ANSWERED: (AGENT_STICKER_ANSWERED, AGENT_STICKER_ANSWERED_COLOR),
}

_sticker_lock = threading.Lock()
# (sticker id, {MARKER_* -> state id}) once resolved. Resolution is attempted
# lazily and retried on the next call while it fails, so a YouGile outage at
# boot doesn't permanently disarm the marker.
_sticker: Optional[Tuple[str, Dict[str, str]]] = None


def _sticker_states(raw: dict) -> Dict[str, str]:
    """Map our marker keys onto the sticker's state ids, by state name."""
    by_name = {
        (s.get("name") or "").strip().lower(): s.get("id")
        for s in (raw.get("states") or [])
        if isinstance(s, dict)
    }
    found: Dict[str, str] = {}
    for marker, (label, _color) in _MARKER_SPECS.items():
        state_id = by_name.get(label.strip().lower())
        if state_id:
            found[marker] = state_id
        else:
            log.warning(
                "agent sticker: '%s' has no state named '%s' — YouGile's API can't "
                "add one, so add it in the UI (or point AGENT_STICKER_%s at an "
                "existing state)",
                AGENT_STICKER_NAME, label, marker.upper(),
            )
    return found


def agent_sticker() -> Optional[Tuple[str, Dict[str, str]]]:
    """The status sticker as (id, {marker -> state id}), creating it if missing.

    Looked up BY NAME rather than by a stored id, and only created when the
    lookup genuinely finds nothing: YouGile has no endpoint that deletes a
    string sticker, so a duplicate created by accident is permanent litter in
    the workspace.
    """
    global _sticker
    if not (AGENT_STATUS_STICKER and AGENT_STICKER_NAME):
        return None
    with _sticker_lock:
        if _sticker is not None:
            return _sticker
        listing = _api_get("/string-stickers?limit=1000", retries=YOUGILE_API_BOOT_RETRIES)
        if listing is None:
            log.warning("agent sticker: can't list stickers — marker disarmed for now")
            return None
        wanted = AGENT_STICKER_NAME.strip().lower()
        raw = next(
            (
                s for s in (listing.get("content") or [])
                if isinstance(s, dict)
                and not s.get("deleted")
                and (s.get("name") or "").strip().lower() == wanted
            ),
            None,
        )
        if raw is None:
            created = _api_post(
                "/string-stickers",
                {
                    "name": AGENT_STICKER_NAME,
                    "icon": AGENT_STICKER_ICON,
                    "states": [
                        {"name": label, "color": color}
                        for label, color in _MARKER_SPECS.values()
                    ],
                },
                retries=YOUGILE_API_BOOT_RETRIES,
            )
            if not (created and created.get("id")):
                log.warning("agent sticker: could not create '%s'", AGENT_STICKER_NAME)
                return None
            log.info("agent sticker: created '%s' (%s)", AGENT_STICKER_NAME, created["id"])
            # POST returns only the id — read it back for the state ids.
            raw = _api_get(
                f"/string-stickers/{created['id']}", retries=YOUGILE_API_BOOT_RETRIES
            )
            if raw is None:
                return None
        states = _sticker_states(raw)
        if not states:
            return None
        _sticker = (raw["id"], states)
        log.info(
            "agent sticker: '%s' (%s) states=%s",
            AGENT_STICKER_NAME, raw["id"], sorted(states),
        )
        return _sticker


def _sticker_snapshot() -> Optional[Dict[str, Any]]:
    """What /healthz reports: the sticker as already resolved, never resolving it
    here — a health check must not depend on the network."""
    if not AGENT_STATUS_STICKER:
        return None
    with _sticker_lock:
        resolved = _sticker
    return {
        "name": AGENT_STICKER_NAME,
        "id": resolved[0] if resolved else None,
        "states": sorted(resolved[1]) if resolved else [],
    }


# task_id -> the marker the task should end up showing (None = clear it). One
# worker thread per task drains this map; a newer value simply overwrites one
# that hasn't been delivered yet. Both properties matter. The API calls stay off
# the request path (they retry a flaky network for as long as it takes), and
# "turn started" / "turn finished" for the same task can never land out of order
# and leave the card lying about whether the agent is busy — the worker always
# applies the most recent intent, not the one it was created with.

_marker_lock = threading.Lock()
_marker_desired: Dict[str, Optional[str]] = {}
_marker_workers: Set[str] = set()


def _apply_task_marker(task_id: str, marker: Optional[str]) -> None:
    """Put the sticker into `marker` (None clears it), skipping the write when
    the card already shows it.

    The read-before-write isn't just thrift: every real change echoes back to us
    as a `task-updated` webhook, so writing only on an actual transition keeps
    the marker from generating an event on every turn. It also means a value a
    human changed by hand mid-turn gets corrected rather than ignored.
    """
    resolved = agent_sticker()
    if resolved is None:
        return
    sticker_id, states = resolved
    value = STICKER_CLEAR if marker is None else states.get(marker)
    if value is None:
        return  # _sticker_states already explained which state is missing
    task = _api_get(f"/tasks/{task_id}")
    if task is None:
        log.warning("agent sticker: can't read task %s — marker=%s not applied", task_id, marker)
        return
    current = (task.get("stickers") or {}).get(sticker_id) or STICKER_CLEAR
    if current == value:
        return
    ok = _api_put(f"/tasks/{task_id}", {"stickers": {sticker_id: value}}) is not None
    log.info("agent sticker: task=%s marker=%s ok=%s", task_id, marker, ok)


def _marker_worker(task_id: str) -> None:
    while True:
        with _marker_lock:
            if task_id not in _marker_desired:
                _marker_workers.discard(task_id)
                return
            marker = _marker_desired.pop(task_id)
        try:
            _apply_task_marker(task_id, marker)
        except Exception:
            log.exception("agent sticker: applying marker=%s to task %s failed", marker, task_id)


def set_task_marker(task_id: Optional[str], marker: Optional[str]) -> None:
    """Show `marker` (MARKER_WORKING / MARKER_ANSWERED, or None to clear) on the
    task card. Fire-and-forget: returns at once, the write happens on the task's
    worker thread."""
    if not (AGENT_STATUS_STICKER and task_id):
        return
    with _marker_lock:
        _marker_desired[task_id] = marker
        if task_id in _marker_workers:
            return  # the live worker will pick this value up on its next lap
        _marker_workers.add(task_id)
    threading.Thread(target=_marker_worker, args=(task_id,), daemon=True).start()


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
            "rule %s: enabled=%s events=%s pattern=%s senders=%d columns=%d "
            "workdir=%s session_per_chat=%s followup_prompt=%s",
            r.name, r.enabled, sorted(r.events),
            r.pattern.pattern if r.pattern else None,
            len(r.allowed_sender_ids), len(r.column_ids), r.workdir,
            r.session_per_chat, bool(r.prompt_template_followup),
        )


refresh_directories()
RULES: List[Rule] = load_rules(RULES_FILE)
resolve_rules(RULES)
# Resolve (or create) the status sticker up front so /healthz can report it and
# the first turn doesn't pay for the lookup. Bounded retries — a dead API at
# boot must not hang the import; the next marker write resolves it lazily.
agent_sticker()

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


# ------------------------------------------------- "who is @Agent?" / replies
#
# Resolution order for the agent's own YouGile account: AGENT_USER_ID ->
# AGENT_USER_EMAIL -> the id learned from a mention chunk (cached in
# state/agent.json). The learning step is what lets a stock install work with no
# configuration at all: the first genuine "@Agent ..." message names the account.

_AGENT_STATE_FILE = STATE_DIR / "agent.json"
_agent_user_lock = threading.Lock()
_agent_user_id: Optional[str] = None


def _cached_agent_user_id() -> Optional[str]:
    try:
        data = json.loads(_AGENT_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    uid = data.get("agent_user_id") if isinstance(data, dict) else None
    return uid if isinstance(uid, str) and _UUID_RE.match(uid) else None


def agent_user_id() -> Optional[str]:
    """The account the agent posts as, or None while it's still unknown."""
    global _agent_user_id
    if AGENT_USER_ID:
        return AGENT_USER_ID
    with _agent_user_lock:
        if _agent_user_id is None:
            _agent_user_id = (
                (USERS_BY_EMAIL.get(AGENT_USER_EMAIL) if AGENT_USER_EMAIL else None)
                or _cached_agent_user_id()
            )
        return _agent_user_id


def learn_agent_user_id(payload) -> None:
    """Pick the agent's user id out of an @-mention chunk and remember it.

    YouGile renders a mention as a chunk
    {"type": "user", "replacement": "@Agent", "data": {"userId": "<uuid>"}}, so
    every real mention identifies the account for free. Cached to
    state/agent.json so a restart doesn't have to wait for the next mention.
    """
    global _agent_user_id
    if not (AGENT_MENTION and isinstance(payload, dict)):
        return
    message = payload.get("payload")
    if not isinstance(message, dict):
        return
    props = message.get("properties")
    params = props.get("params") if isinstance(props, dict) else None
    chunks = params.get("chunks") if isinstance(params, dict) else None
    if not isinstance(chunks, list):
        return
    for chunk in chunks:
        if not isinstance(chunk, dict) or chunk.get("type") != "user":
            continue
        if (chunk.get("replacement") or "").strip() != AGENT_MENTION:
            continue
        data = chunk.get("data")
        uid = data.get("userId") if isinstance(data, dict) else None
        if not (isinstance(uid, str) and _UUID_RE.match(uid)):
            continue
        with _agent_user_lock:
            if _agent_user_id == uid:
                return
            _agent_user_id = uid
        try:
            _AGENT_STATE_FILE.write_text(
                json.dumps({"agent_user_id": uid}, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            log.exception("agent identity: could not cache %s", _AGENT_STATE_FILE)
        log.info("agent identity: %s resolved to user %s", AGENT_MENTION, uid)
        return


def note_reply_formatting(payload) -> None:
    """Record whether the agent's own last message reached the chat formatted.

    The receiver never sends the agent's replies — the agent posts them over MCP
    and we only see them come back as deliveries — so this is the single point
    where "the report went out as an unformatted wall" is observable at all.
    Nothing can be done to the message itself: YouGile's message PUT only takes
    deleted/label/react, so a plain-text reply cannot be re-rendered after the
    fact. The fix has to be prospective — flag it on the chat and let the next
    prompt say so, since the agent has no other way to learn how its own
    messages look.

    Only long messages count: acks and one-liners are plain text by design.
    """
    if not isinstance(payload, dict) or payload.get("event") != "chat_message-created":
        return
    me = agent_user_id()
    if not me or payload.get("fromUserId") != me:
        return
    chat_id = extract_chat_id(payload)
    message = payload.get("payload")
    if not (chat_id and isinstance(message, dict)):
        return
    # YouGile carries the rendered body in `properties.textHtml`, not at the top
    # level of the message the webhook delivers.
    props = message.get("properties")
    raw = props.get("textHtml") if isinstance(props, dict) else None
    html = raw if isinstance(raw, str) else ""
    text = message.get("text") or ""

    # Written either way: one clean reply clears a flag, so a notice never
    # outlives the mistake it describes.
    updates: Dict[str, Any] = {}

    # A CDATA wrapper breaks a message at ANY length, so unlike the plain-text
    # check below this one is not excused by brevity. Only messages that carry
    # `textHtml` are judged: one without it cannot be CDATA-broken, and must not
    # clear the flag a broken reply just set.
    if html.strip():
        cdata = "<![CDATA[" in html
        if cdata:
            log.warning(
                "chat %s: agent posted a reply with CDATA-wrapped textHtml — "
                "YouGile shows it as an empty bubble; the next prompt will say "
                "so (our MCP server should have caught this)",
                chat_id,
            )
        updates["last_reply_cdata"] = cdata

    # Only long messages count here: acks and one-liners are plain text by
    # design, and must not clear the flag the real reply set.
    if len(text) >= PLAIN_REPLY_MIN_CHARS:
        plain = not html.strip()
        if plain:
            log.warning(
                "chat %s: agent posted a %d-char reply with no textHtml — the "
                "user sees it unformatted; the next prompt will say so",
                chat_id, len(text),
            )
        updates["last_reply_plain"] = plain

    # Never for an unknown chat — the state file's existence is what
    # `skip_if_chat_known` reads as "we have run here", and a bookkeeping write
    # must not silence a future first mention.
    if updates and chat_known(chat_id):
        update_chat_state(chat_id, **updates)


def _fetch_message(chat_id: str, message_id: str) -> Optional[dict]:
    """Read a single chat message. Deliberately a SMALL retry budget: this runs
    on the request path, before the rules are even evaluated, and YouGile
    re-delivers a webhook it considers slow."""
    msg = _api_get(
        f"/chats/{chat_id}/messages/{message_id}", retries=YOUGILE_API_BOOT_RETRIES
    )
    return msg if isinstance(msg, dict) else None


def annotate_agent_reply(payload) -> bool:
    """Treat a reply to one of the agent's own messages as addressing the agent.

    Replying to (or forwarding) an agent message is the natural way to say "you,
    keep going" — but YouGile only records `properties.replyTo = <message id>`
    and the text carries no mention, so no `@Agent` rule would match it. We
    resolve that message and, when the agent is its author, inject AGENT_MENTION
    into the payload under `_addressedToAgent` — the existing rules then fire on
    it unchanged, and the spawned agent sees in `{event_json}` both why it was
    triggered and what the user was replying to.

    Mutates `payload` in place. Returns True when it marked the payload. Costs
    one API call, and only for messages that actually carry a replyTo.
    """
    if not (REPLY_TO_AGENT_IS_MENTION and AGENT_MENTION):
        return False
    if not isinstance(payload, dict):
        return False
    if not str(payload.get("event") or "").startswith("chat_message-"):
        return False
    message = payload.get("payload")
    if not isinstance(message, dict):
        return False
    props = message.get("properties")
    reply_to = props.get("replyTo") if isinstance(props, dict) else None
    if reply_to is None or not str(reply_to).isdigit():
        return False
    if AGENT_MENTION in (message.get("text") or ""):
        return False  # already an explicit mention — nothing to add
    chat_id = extract_chat_id(payload)
    agent_id = agent_user_id()
    if not chat_id:
        return False
    if not agent_id:
        log.info(
            "reply-to-agent: agent account still unknown (no mention seen yet) — "
            "set AGENT_USER_EMAIL to enable this immediately"
        )
        return False
    replied_to = _fetch_message(chat_id, str(reply_to))
    if replied_to is None or replied_to.get("fromUserId") != agent_id:
        return False
    quoted = (replied_to.get("text") or "")[:REPLY_QUOTE_MAX_CHARS]
    payload["_addressedToAgent"] = {
        "mention": AGENT_MENTION,
        "reason": "the user replied to a message the agent posted",
        "replyToMessageId": str(reply_to),
        "replyToText": quoted,
    }
    log.info(
        "reply-to-agent: chat=%s replyTo=%s — treating as %s",
        chat_id, reply_to, AGENT_MENTION,
    )
    return True


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
        # A turn that is being launched right now counts as an existing session
        # too — state/chats/<chatId>.json only lands mid-spawn (after the chat
        # history fetch, which retries a flaky network for as long as it takes),
        # so for the first @Agent turn there is a multi-second window where the
        # chat still looks unknown on disk. A move to "In progress" landing in
        # that window used to sail through this check. spawn_claude re-checks
        # under the chat lock, which is what actually makes this race-free.
        if chat_id and chat_turn_in_flight(chat_id):
            return False, "chat-turn-in-flight"
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


# ------------------------------------------------------------- turn records
#
# One file per chat, alive only while that chat has a turn running. It holds
# everything finish_turn() needs, so finishing does not depend on the process
# that started the turn still being around: the pid to watch, where its output
# went, the watermark to roll back if the session turns out to be gone, and the
# instruction queued behind an interrupt. Written after the process is live —
# a launch that never happened must not leave a record claiming a running turn.


def _turn_path(chat_id: str) -> Path:
    return TURNS_STATE_DIR / f"{chat_id}.json"


def read_turn(chat_id: str) -> Dict[str, Any]:
    p = _turn_path(chat_id)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.exception("turn record corrupted: %s", p)
        return {}


def write_turn(chat_id: str, **fields: Any) -> Dict[str, Any]:
    p = _turn_path(chat_id)
    existing = read_turn(chat_id)
    existing.update(fields)
    # Always present, so a record read back on its own knows whose it is.
    existing["chat_id"] = chat_id
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return dict(existing)


def clear_turn(chat_id: str) -> None:
    try:
        _turn_path(chat_id).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        log.exception("could not remove turn record for chat %s", chat_id)


def list_open_turns() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for p in sorted(TURNS_STATE_DIR.glob("*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.warning("turn record unreadable, removing: %s", p)
            try:
                p.unlink()
            except OSError:
                pass
            continue
        rec.setdefault("chat_id", p.stem)
        out.append(rec)
    return out


def _pid_alive(pid: Optional[int]) -> bool:
    """Is this pid still around? Signal 0 asks without delivering anything.

    A pid the receiver no longer owns cannot be wait()ed on, so this is the only
    way to supervise a turn adopted after a restart.
    """
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, just not ours to signal
    except OSError:
        return False
    return True


def _read_turn_output(log_path: Optional[str]) -> str:
    """Head + tail of a turn's output file, for the failure report.

    Startup errors are printed first and the answer last; the middle is tool
    traffic no report needs. Reading it back at the end costs one open, and
    unlike an in-memory buffer it survives the receiver restarting.
    """
    if not log_path:
        return ""
    p = Path(log_path)
    try:
        size = p.stat().st_size
        with p.open("r", encoding="utf-8", errors="replace") as f:
            head = f.read(CLAUDE_CAPTURE_HEAD_CHARS)
            if size <= CLAUDE_CAPTURE_HEAD_CHARS + CLAUDE_CAPTURE_TAIL_CHARS:
                return head + f.read()
            dropped = size - CLAUDE_CAPTURE_HEAD_CHARS - CLAUDE_CAPTURE_TAIL_CHARS
            f.seek(max(0, size - CLAUDE_CAPTURE_TAIL_CHARS))
            tail = f.read()
        return f"{head}\n…[{dropped} chars omitted]…\n{tail}"
    except OSError:
        return ""


def _reap_turn_logs() -> None:
    """Keep the turn-log directory from growing without bound."""
    try:
        logs = sorted(TURN_LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for stale in logs[:-TURN_LOG_KEEP] if len(logs) > TURN_LOG_KEEP else []:
        try:
            stale.unlink()
        except OSError:
            pass


def agent_replied_since(chat_id: str, since_id: Optional[int]) -> Optional[bool]:
    """Did the agent post anything into the chat after `since_id`?

    The receiver never writes the agent's answers — the agent posts them itself
    over MCP — so this is the only way to tell "the turn ended" from "the user
    got an answer". Returns None when the question can't be answered (no agent
    account resolved, or the API is unreachable), so callers can stay quiet
    rather than accuse a turn that did reply.
    """
    me = agent_user_id()
    if not me:
        return None
    params = {"limit": "50", "offset": "0", "includeSystem": "false"}
    if since_id is not None:
        params["since"] = str(int(since_id) + 1)
    qs = urllib.parse.urlencode(params)
    # Straight to the API rather than fetch_chat_messages(): that one folds an
    # unreachable API into an empty list, and "we couldn't ask" must not be
    # reported to the user as "the agent said nothing".
    data = _api_get(f"/chats/{chat_id}/messages?{qs}")
    if data is None:
        return None
    return any(
        m.get("fromUserId") == me for m in (data.get("content") or [])
    )


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

    `limit <= 0` means "everything after `since`, uncapped" — the caller then
    decides what to do with an oversized batch. That matters for the delta
    fetch: silently keeping the newest N and advancing the watermark past the
    rest would drop messages the agent never saw.
    """
    PAGE = 1000  # YouGile API max
    page_limit = PAGE if limit <= 0 else min(PAGE, limit)
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
    if limit > 0 and len(collected) > limit:
        collected = collected[-limit:]
    return collected


def merge_trigger_message(
    messages: List[dict], payload: Any, since: Optional[int]
) -> List[dict]:
    """Make sure the message that fired this turn is in the batch.

    The webhook routinely beats /chats/<id>/messages to the punch, so the very
    message we are reacting to can be missing from the fetch. If it is, its id
    never enters the watermark and the next turn serves it again — which is
    exactly how a chat ends up re-reading messages it has already answered.
    """
    inner = payload.get("payload") if isinstance(payload, dict) else None
    if not isinstance(inner, dict):
        return messages
    mid = inner.get("id")
    if not mid:
        return messages
    if since is not None and int(mid) <= int(since):
        return messages  # already delivered by an earlier turn
    if any(m.get("id") == mid for m in messages):
        return messages
    trigger = dict(inner)
    trigger.setdefault("fromUserId", payload.get("fromUserId"))
    merged = messages + [trigger]
    merged.sort(key=lambda m: m.get("id") or 0)
    return merged


def _format_sender(user_id: Optional[str]) -> str:
    if not user_id:
        return "unknown"
    u = USERS_BY_ID.get(user_id)
    if not u:
        return f"user:{user_id[:8]}"
    return u.get("email") or u.get("realName") or f"user:{user_id[:8]}"


def render_chat_history(
    messages: List[dict], chat_id: Optional[str] = None,
    exclude_agent: bool = False,
) -> Tuple[str, List[Path]]:
    """Render messages as text. Downloads any `/root/#file:<url>` attachments
    to state/attachments/<chatId>/ and rewrites them inline as
    `[image: <abs-path>]` / `[file: <abs-path>]`. Returns (rendered_text,
    sorted list of unique local attachment paths referenced anywhere in the
    rendered history).

    `exclude_agent` drops the agent's own posts. Set it when the session being
    resumed already holds them: it wrote those replies itself a few minutes ago
    and they are still in its context, so feeding them back re-reads its own
    words at full price — in practice 90%+ of a follow-up's history block. The
    first turn of a session is the opposite case: there the agent has no memory
    and its past replies are the only record of what it already answered.
    """
    if not messages:
        return "(no prior messages)", []
    me = agent_user_id() if exclude_agent else None
    lines: List[str] = []
    all_paths: List[Path] = []
    seen_paths: Set[str] = set()
    for m in messages:
        # Slash commands and our own replies to them are plumbing, not
        # conversation — keep them out of the agent's context.
        if _is_command_noise(m.get("text") or ""):
            continue
        if me and m.get("fromUserId") == me:
            continue
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
    if not lines:
        # Everything in the batch was ours or was plumbing. Say so rather than
        # handing the agent an empty block it has to interpret.
        return "(no new messages)", all_paths
    return "\n".join(lines), all_paths


# ------------------------------------------------- cost accounting / metrics
#
# Everything here reads claude's own on-disk transcript
# (~/.claude/projects/<slugified cwd>/<session id>.jsonl) — the same file
# `claude --resume` reads. Each assistant message carries a `usage` block, and
# the last one tells us the size of the session's context and whether the turn
# that produced it hit the prompt cache. Both are free to obtain and are the
# only inputs the rotation decision needs.


def _fmt_int(n: Optional[int]) -> str:
    return "?" if n is None else f"{n:,}".replace(",", " ")


def _fmt_usd(x: Optional[float]) -> str:
    if x is None:
        return "?"
    return f"${x:,.2f}".replace(",", " ") if x >= 10 else f"${x:.2f}"


def _plural_form(n: Any) -> str:
    """Which of the three Slavic plural forms `n` takes: 1 ход, 2 хода, 5 ходов.

    The forms themselves live in the messages file; a language that doesn't work
    this way just repeats the same word in all three keys.
    """
    try:
        n = abs(int(n))
    except (TypeError, ValueError):
        return "many"
    if n % 10 == 1 and n % 100 != 11:
        return "one"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "few"
    return "many"


def _fmt_count(n: Any, kind: str) -> str:
    """"61 ход" / "623 хода" — a number with its noun in the right form."""
    return f"{_fmt_int(n)} {msg(f'plural.{kind}_{_plural_form(n)}')}"


def _fmt_when(iso: Optional[str]) -> str:
    """An ISO timestamp as "02.08 09:30" — a chat line has no room for more."""
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return msg("age.unknown")
    return dt.astimezone().strftime("%d.%m %H:%M")


def _fmt_age(minutes: Optional[float]) -> str:
    if minutes is None:
        return msg("age.unknown")
    if minutes < 1:
        return msg("age.just_now")
    if minutes < 60:
        return msg("age.minutes", minutes=int(minutes))
    if minutes < 60 * 24:
        h, m = divmod(int(minutes), 60)
        return (msg("age.hours", hours=h, minutes=m) if m
                else msg("age.hours_flat", hours=h))
    d, h = divmod(int(minutes) // 60, 24)
    return (msg("age.days", days=d, hours=h) if h
            else msg("age.days_flat", days=d))


def usage_cost(u: Dict[str, Any]) -> float:
    """Dollar cost of a single API call, from its `usage` block."""
    read = u.get("cache_read_input_tokens", 0) or 0
    write = u.get("cache_creation_input_tokens", 0) or 0
    plain = u.get("input_tokens", 0) or 0
    out = u.get("output_tokens", 0) or 0
    return (
        (plain + write * CACHE_WRITE_MULT + read * CACHE_READ_MULT)
        * PRICE_IN_PER_MTOK
        + out * PRICE_OUT_PER_MTOK
    ) / 1_000_000


def _transcript_path(workdir: str, session_id: str) -> Path:
    """claude slugifies the absolute cwd by replacing "/" and "." with "-"."""
    slug = str(Path(workdir).resolve()).replace("/", "-").replace(".", "-")
    return Path.home() / ".claude" / "projects" / slug / f"{session_id}.jsonl"


def _turn_from_usage(u: Dict[str, Any], ts: Optional[str]) -> Dict[str, Any]:
    read = u.get("cache_read_input_tokens", 0) or 0
    write = u.get("cache_creation_input_tokens", 0) or 0
    plain = u.get("input_tokens", 0) or 0
    ctx = read + write + plain
    return {
        "at": ts,
        "ctx_tokens": ctx,
        "cache_read": read,
        "cache_creation": write,
        "input_tokens": plain,
        "output_tokens": u.get("output_tokens", 0) or 0,
        # A miss re-writes the conversation while the shared tools/system prefix
        # (a stable 15-25k) still reads from cache — hence the ratio test rather
        # than "read == 0".
        "cold": ctx > 50_000 and write > 0.5 * ctx,
        "cost_usd": round(usage_cost(u), 4),
    }


def _usage_tail(path: Path, tail_bytes: int = 262_144) -> Optional[Dict[str, Any]]:
    """Last turn's usage, read from the tail of the file.

    Transcripts reach 10-14 MB; seeking to the end keeps this O(256 KB) so it
    can run on every spawn without thinking about it.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return None
    try:
        with path.open("rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                f.readline()  # drop the partial first line
            chunk = f.read()
    except OSError:
        log.exception("failed to read transcript tail: %s", path)
        return None
    found: Optional[Dict[str, Any]] = None
    for raw in chunk.splitlines():
        if b'"usage"' not in raw:
            continue
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        u = (d.get("message") or {}).get("usage")
        if isinstance(u, dict):
            found = _turn_from_usage(u, d.get("timestamp"))
    return found


_totals_cache: "OrderedDict[Tuple[str, int, int], Dict[str, Any]]" = OrderedDict()


def _usage_totals(path: Path) -> Dict[str, Any]:
    """Whole-session totals. Full scan, memoised on (path, mtime, size).

    ~1s on a 13 MB transcript thanks to the substring pre-filter — only called
    when a turn ends, on rotation, and by /status.

    One API response is written to the transcript once per content block, so a
    reply with thinking + text + two tool calls appears as four lines carrying
    the SAME `usage`. Summing them blind counts the response that many times —
    it read one real session as 261 calls / $38.69 when it was 170 / $26.23.
    Hence the dedupe on the message id: `calls` is API calls, not lines.
    """
    empty = {
        "calls": 0, "write_tokens": 0, "read_tokens": 0,
        "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "cold_turns": 0,
    }
    try:
        st = path.stat()
    except OSError:
        return empty
    key = (str(path), st.st_mtime_ns, st.st_size)
    hit = _totals_cache.get(key)
    if hit is not None:
        _totals_cache.move_to_end(key)
        return hit
    tot = dict(empty)
    seen: set = set()
    try:
        with path.open("rb") as f:
            for raw in f:
                if b'"usage"' not in raw:
                    continue
                try:
                    d = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                m = d.get("message") or {}
                u = m.get("usage")
                if not isinstance(u, dict):
                    continue
                mid = m.get("id")
                # No id (shouldn't happen) — count it, losing a duplicate is
                # worse than counting a one-off twice.
                if mid is not None:
                    if mid in seen:
                        continue
                    seen.add(mid)
                t = _turn_from_usage(u, None)
                tot["calls"] += 1
                tot["write_tokens"] += t["cache_creation"]
                tot["read_tokens"] += t["cache_read"]
                tot["input_tokens"] += t["input_tokens"]
                tot["output_tokens"] += t["output_tokens"]
                tot["cost_usd"] += t["cost_usd"]
                tot["cold_turns"] += 1 if t["cold"] else 0
    except OSError:
        log.exception("failed to scan transcript: %s", path)
        return empty
    tot["cost_usd"] = round(tot["cost_usd"], 4)
    tot["write_usd"] = round(
        tot["write_tokens"] * CACHE_WRITE_MULT * PRICE_IN_PER_MTOK / 1e6, 4)
    tot["read_usd"] = round(
        tot["read_tokens"] * CACHE_READ_MULT * PRICE_IN_PER_MTOK / 1e6, 4)
    tot["output_usd"] = round(tot["output_tokens"] * PRICE_OUT_PER_MTOK / 1e6, 4)
    _totals_cache[key] = tot
    while len(_totals_cache) > 32:
        _totals_cache.popitem(last=False)
    return tot


def _extract_skeleton(path: Path, max_chars: int = HANDOFF_SKELETON_MAX_CHARS) -> str:
    """What the previous session concluded, without the tool noise.

    A multi-megabyte transcript is almost entirely tool_use/tool_result/thinking;
    the few structural markers (assistant replies, todos, edited files, PR links)
    compress to a few KB, mechanically and for free.

    Deliberately skips `user` messages: in `-p` mode those are the prompts THIS
    receiver generated, plus skill bodies injected into the turn — not anything
    a human said. The human's actual words reach the handoff from YouGile, so
    including them here would duplicate the chat and bury the signal.
    """
    assists: List[str] = []
    todos: str = ""
    edited: "OrderedDict[str, None]" = OrderedDict()
    prs: "OrderedDict[str, None]" = OrderedDict()
    compacts: List[str] = []
    try:
        with path.open("rb") as f:
            for raw in f:
                try:
                    d = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                t = d.get("type")
                if t == "pr-link":
                    url = d.get("prUrl")
                    if url:
                        prs[f"{d.get('prRepository', '?')} — {url}"] = None
                    continue
                msg = d.get("message") or {}
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                if d.get("isCompactSummary"):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "text":
                            compacts.append(b.get("text", ""))
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text" and t == "assistant":
                        assists.append(b.get("text", ""))
                    elif bt == "tool_use":
                        name = b.get("name") or ""
                        inp = b.get("input") or {}
                        if name in ("Edit", "Write", "NotebookEdit") and isinstance(inp, dict):
                            fp = inp.get("file_path")
                            if fp:
                                edited[str(fp)] = None
                        elif name == "TodoWrite" and isinstance(inp, dict):
                            items = inp.get("todos") or []
                            if isinstance(items, list):
                                todos = "\n".join(
                                    f"  [{i.get('status', '?')}] {i.get('content', '')}"
                                    for i in items if isinstance(i, dict)
                                )
    except OSError:
        log.exception("failed to read transcript for skeleton: %s", path)
        return "(transcript unreadable)"

    def _clip(items: List[str], limit: int, chars: int) -> List[str]:
        out = []
        for x in items:
            x = (x or "").strip()
            if not x:
                continue
            out.append(x if len(x) <= chars else x[:chars] + " …(truncated)")
        return out[-limit:]

    parts: List[str] = []
    if compacts:
        parts.append("### Auto-compaction summaries\n\n" + "\n\n".join(compacts[-2:]))
    # The running commentary between tool calls ("let me check X", "false
    # positive — …") is process, not state: it is the most expensive section of
    # the handoff and the least useful, because the conclusions it reaches are
    # already in the chat in cleaned-up form. Off by default; the last few lines
    # are worth keeping only to show where the session was cut off.
    if assists and HANDOFF_NARRATION > 0:
        parts.append(
            "### What the previous session said, most recent last\n\n"
            + "\n\n".join(f"- {x}" for x in _clip(assists, HANDOFF_NARRATION, 2000)))
    if todos:
        parts.append("### Final todo state\n\n```\n" + todos + "\n```")
    if edited:
        parts.append("### Files the previous session edited\n\n" + "\n".join(
            f"- {p}" for p in list(edited)[:200]))
    if prs:
        parts.append("### Pull requests it opened\n\n" + "\n".join(
            f"- {p}" for p in prs))
    out = "\n\n".join(parts)
    if len(out) > max_chars:
        out = out[:max_chars] + "\n\n…(skeleton truncated)"
    return out or "(no dialogue recovered from the transcript)"


# --------------------------------------------------- cache-hit statistics
#
# p(cache miss | idle) measured on this machine, seeded with the values observed
# across 88 turns so the estimator is calibrated from the very first rotation
# and self-corrects as MCP-server reliability changes.

_IDLE_BUCKETS = ((10.0, "0-10"), (30.0, "10-30"), (60.0, "30-60"), (float("inf"), "60+"))
_CACHE_STATS_SEED = {
    "0-10": {"turns": 45, "misses": 3},
    "10-30": {"turns": 21, "misses": 5},
    "30-60": {"turns": 4, "misses": 3},
    "60+": {"turns": 18, "misses": 18},
}
_cache_stats_lock = threading.Lock()


def _idle_bucket(idle_min: Optional[float]) -> str:
    if idle_min is None:
        return "60+"  # unknown age — assume the pessimistic case
    for edge, name in _IDLE_BUCKETS:
        if idle_min < edge:
            return name
    return "60+"


def _load_stats_file() -> Dict[str, Any]:
    try:
        data = json.loads(CACHE_STATS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_cache_stats() -> Dict[str, Dict[str, int]]:
    buckets = _load_stats_file().get("buckets") or {}
    merged = {k: dict(v) for k, v in _CACHE_STATS_SEED.items()}
    for k, v in buckets.items():
        if k in merged and isinstance(v, dict):
            merged[k] = {"turns": int(v.get("turns", 0)), "misses": int(v.get("misses", 0))}
    return merged


def _save_stats(**fields: Any) -> None:
    """Merge into the stats file — the two estimators share it and are written
    from different code paths, so a blind overwrite would drop the other one."""
    data = _load_stats_file()
    data.update(fields)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = CACHE_STATS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CACHE_STATS_FILE)


def calls_per_turn() -> float:
    """How many API calls one agent turn makes, on this machine, lately."""
    try:
        n = float(_load_stats_file().get("calls_per_turn") or 0)
    except (TypeError, ValueError):
        n = 0.0
    return n if n > 0 else ROTATE_CALLS_PER_TURN


def record_calls_per_turn(calls: int) -> None:
    """One observation of "how long was that turn". EWMA, not a mean: turn
    length swings hard by request type (a "готово" is 22 calls, an implement is
    106) and what we want is what the NEXT one is likely to look like."""
    if calls <= 0:
        return
    with _cache_stats_lock:
        prev = calls_per_turn()
        # Clamped so one runaway turn can't park the estimator at an absurd
        # value and rotate every session on sight.
        nxt = min(200.0, max(5.0, prev * 0.75 + calls * 0.25))
        try:
            _save_stats(calls_per_turn=round(nxt, 1))
        except OSError:
            log.exception("failed to persist calls-per-turn stat")


def miss_probability(bucket: str) -> float:
    b = _load_cache_stats().get(bucket) or {"turns": 0, "misses": 0}
    # Laplace smoothing keeps a thin bucket from swinging to 0 or 1.
    return (b["misses"] + 1) / (b["turns"] + 2)


def record_turn_outcome(idle_min: Optional[float], cold: bool) -> None:
    """One observation of "did the cache survive this pause?"."""
    bucket = _idle_bucket(idle_min)
    with _cache_stats_lock:
        buckets = _load_cache_stats()
        b = buckets.setdefault(bucket, {"turns": 0, "misses": 0})
        b["turns"] += 1
        b["misses"] += 1 if cold else 0
        # Halve when a bucket gets fat so it tracks current behaviour rather
        # than averaging over months of history.
        if b["turns"] > 400:
            b["turns"] //= 2
            b["misses"] //= 2
        try:
            _save_stats(buckets=buckets)
        except OSError:
            log.exception("failed to persist cache stats")


def expected_turn_cost(ctx_tokens: Optional[int], idle_min: Optional[float]) -> Optional[float]:
    """What the next TURN's input will cost, before we spend anything on it.

    A turn is `calls` API calls, and each one re-sends the prefix. Only the
    first can miss the cache, so the write premium is paid once while the read
    is paid every call — which is why turn length, not idle time, dominates the
    figure once a session gets fat.
    """
    if not ctx_tokens:
        return None
    p = miss_probability(_idle_bucket(idle_min))
    rate = (calls_per_turn() * CACHE_READ_MULT
            + p * (CACHE_WRITE_MULT - CACHE_READ_MULT))
    return ctx_tokens * rate * PRICE_IN_PER_MTOK / 1_000_000


def _record_turn_metrics(
    chat_id: str, session_id: str, rule: "Rule", idle_before: Optional[float]
) -> None:
    """After a turn: persist what it cost and whether its cache survived.

    Runs in the reaper thread, so a slow full-transcript scan here delays
    nothing the user is waiting on.
    """
    try:
        path = _transcript_path(rule.workdir, session_id)
        last = _usage_tail(path)
        if last is None:
            return
        # Before the update: the session's call count as of the PREVIOUS turn.
        # The difference is how many calls this turn made — the only place that
        # number is observable, and the input to the next rotation decision.
        before = ((get_chat_state(chat_id).get("session_totals") or {})
                  .get("calls") or 0)
        totals = _usage_totals(path)
        update_chat_state(chat_id, last_turn=last, session_totals=totals)
        record_turn_outcome(idle_before, bool(last.get("cold")))
        record_calls_per_turn((totals.get("calls") or 0) - before)
        log.info(
            "chat %s: turn ctx=%s calls=%s cold=%s cost=%.3f session=%.2f",
            chat_id, last.get("ctx_tokens"),
            (totals.get("calls") or 0) - before, last.get("cold"),
            last.get("cost_usd") or 0.0, totals.get("cost_usd") or 0.0,
        )
    except Exception:
        log.exception("failed to record turn metrics for chat %s", chat_id)


def render_rotation_advice(chat_id: str, facts: Dict[str, Any]) -> str:
    """`manual` mode: flag the threshold, let the human decide.

    Returns the line to fold into this turn's ack, once per session — repeating
    "you should rotate" on every turn would train the user to ignore it.
    """
    state = get_chat_state(chat_id)
    if state.get("advisory_at_seq") == facts["session"]["seq"]:
        return ""  # already told them about this session
    update_chat_state(chat_id, advisory_at_seq=facts["session"]["seq"])
    return msg(
        "advisory.line",
        prefix=COMMAND_REPLY_PREFIX,
        tokens=_fmt_int(facts.get("ctx_tokens")),
        cost=_fmt_usd(facts.get("expected_cost")),
        threshold=_fmt_usd(ROTATE_TURN_COST_USD),
    )


# ------------------------------------------------------------- session chain
#
# The session id used to BE the chat id, which made rotation impossible. Now a
# chat owns a chain of sessions: seq 0 keeps the chat id (so every existing chat
# carries on untouched with no migration) and later links are derived with
# uuid5, which is deterministic and — unlike a random uuid4 — needs no lookup
# table. `claude --session-id` requires a valid UUID, which uuid5 satisfies.

SESSION_NS = uuid.UUID("b1f0c3a2-5d47-4e9a-9c31-8f2e6d0a7b54")


def session_for(chat_id: str, seq: int) -> str:
    if seq <= 0:
        return chat_id
    return str(uuid.uuid5(SESSION_NS, f"yougile:{chat_id}:{seq}"))


def session_view(chat_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
    """Normalised session pointer, tolerating pre-rotation state files."""
    seq = int(state.get("session_seq", 0) or 0)
    return {
        "seq": seq,
        "id": state.get("session_id") or session_for(chat_id, seq),
        # A state file that predates rotation describes a live seq-0 session.
        "bootstrapped": bool(state.get("session_bootstrapped", bool(state))),
    }


def session_facts(chat_id: str, state: Dict[str, Any], rule: "Rule") -> Dict[str, Any]:
    """Everything the rotation decision and /status need — all of it free."""
    sv = session_view(chat_id, state)
    tail = _usage_tail(_transcript_path(rule.workdir, sv["id"]))
    last = tail or state.get("last_turn") or {}
    ctx = last.get("ctx_tokens")
    at = last.get("at") or state.get("updated_at")
    idle_min: Optional[float] = None
    if at:
        try:
            then = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
            idle_min = (datetime.now(timezone.utc) - then).total_seconds() / 60
        except ValueError:
            idle_min = None
    bucket = _idle_bucket(idle_min)
    return {
        "session": sv,
        "ctx_tokens": ctx,
        "idle_min": idle_min,
        "bucket": bucket,
        "p_miss": miss_probability(bucket),
        "expected_cost": expected_turn_cost(ctx, idle_min),
        "last_turn": last,
    }


def should_rotate(
    chat_id: str, state: Dict[str, Any], rule: "Rule", interrupted: bool
) -> Tuple[bool, str, Dict[str, Any]]:
    """(rotate?, reason, facts). Purely a-priori — costs nothing to ask."""
    facts = session_facts(chat_id, state, rule)
    if not facts["session"]["bootstrapped"]:
        # Nothing to rotate away from. A /new issued in this window (right
        # after a rotation, before the new session's first turn) is already
        # satisfied — drop the flag so it can't fire again one turn later.
        if state.get("pending_rotation") or state.get("skip_rotation_once"):
            update_chat_state(chat_id, pending_rotation=False,
                              skip_rotation_once=False)
        return False, "", facts
    # An explicit /new wins over everything, including an interrupt.
    if state.get("pending_rotation"):
        return True, "command", facts
    if state.get("skip_rotation_once"):
        return False, "kept", facts
    if SESSION_ROTATE_MODE == "off":
        return False, "", facts
    # Mid-flight redirects must keep their continuity; the interrupted turn is
    # resumed, not replaced.
    if interrupted:
        return False, "", facts
    ctx = facts["ctx_tokens"]
    if not ctx or ctx < ROTATE_MIN_CTX:
        return False, "", facts
    over = ctx >= ROTATE_HARD_CTX
    cost = facts["expected_cost"]
    reason = "hard-ctx" if over else (
        "cost" if (cost is not None and cost > ROTATE_TURN_COST_USD) else "")
    if not reason:
        return False, "", facts
    # `manual` notices the threshold but leaves the decision to the human.
    if SESSION_ROTATE_MODE != "auto":
        return False, "advisory:" + reason, facts
    return True, reason, facts


def rotate_session(
    chat_id: str, state: Dict[str, Any], reason: str, facts: Dict[str, Any], rule: "Rule"
) -> Dict[str, Any]:
    """Retire the current session, point the chat at the next one."""
    sv = facts["session"]
    totals = _usage_totals(_transcript_path(rule.workdir, sv["id"]))
    retired = {
        "seq": sv["seq"],
        "id": sv["id"],
        "started_at": (state.get("sessions") or [{}])[-1].get("started_at")
        if state.get("sessions") else state.get("first_seen_at"),
        "retired_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "ctx_tokens": facts.get("ctx_tokens"),
        "calls": totals.get("calls"),
        "cost_usd": totals.get("cost_usd"),
    }
    chain = list(state.get("sessions") or [])
    # Replace the open entry for this seq if we already recorded one.
    chain = [s for s in chain if s.get("seq") != sv["seq"]] + [retired]
    new_seq = sv["seq"] + 1
    return update_chat_state(
        chat_id,
        session_seq=new_seq,
        session_id=session_for(chat_id, new_seq),
        session_bootstrapped=False,
        sessions=chain,
        # The new link starts empty. Leaving the retired session's figures here
        # would make the fresh session report the old context and price its
        # first turn off it.
        session_totals={},
        last_turn={},
        pending_rotation=False,
        skip_rotation_once=False,
        rotated_at=datetime.now(timezone.utc).isoformat(),
        rotated_from=sv["id"],
        rotate_reason=reason,
    )


# ------------------------------------------------------------------ workspace
#
# The `feature` skill records its worktree/branch/PR state in
# worktrees/<slug>/.feature.json, keyed by the claude session id. Rotation
# changes that id, so we re-point the file (and add chat_id, which never
# changes) — otherwise the next turn would fail to resume the same branch and
# would open a second PR.


def _git(args: List[str], cwd: Path, timeout: int = 15) -> str:
    try:
        p = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True,
            text=True, timeout=timeout,
        )
        return (p.stdout or "").strip() if p.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def find_workspace(workdir: str, session_ids: List[str], chat_id: str) -> Optional[Path]:
    root = Path(workdir) / "worktrees"
    if not root.is_dir():
        return None
    wanted = {s for s in session_ids if s} | {chat_id}
    for fj in sorted(root.glob("*/.feature.json")):
        try:
            data = json.loads(fj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("session_id") in wanted or data.get("chat_id") in wanted:
            return fj.parent
    return None


def patch_feature_json(ws: Path, session_id: str, chat_id: str) -> None:
    fj = ws / ".feature.json"
    try:
        data = json.loads(fj.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    data["session_id"] = session_id
    data["chat_id"] = chat_id
    tmp = fj.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(fj)
    except OSError:
        log.exception("failed to patch %s", fj)


def workspace_facts(ws: Optional[Path]) -> Tuple[str, Dict[str, Any]]:
    """Markdown block + parsed .feature.json for the handoff and /status."""
    if ws is None:
        return "(no `feature` workspace found for this chat)", {}
    try:
        data = json.loads((ws / ".feature.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return f"(workspace at {ws}, .feature.json unreadable)", {}
    lines = [f"Workspace: `{ws}`", f"Slug: `{data.get('task', ws.name)}`", ""]
    for name, r in (data.get("repos") or {}).items():
        repo = ws / name
        branch, base = r.get("branch"), r.get("base")
        lines.append(f"**{name}** — branch `{branch}` off `{base}`")
        if r.get("pr"):
            lines.append(f"  PR: {r['pr']}")
        if r.get("url"):
            lines.append(f"  dev server: {r['url']}")
        if repo.is_dir() and branch and base:
            logs = _git(["log", "--oneline", f"origin/{base}..HEAD"], repo)
            stat = _git(["diff", "--stat", f"origin/{base}...HEAD"], repo)
            if logs:
                lines.append("  commits:\n" + "\n".join(f"    {l}" for l in logs.splitlines()))
            if stat:
                lines.append("  diff --stat:\n" + "\n".join(f"    {l}" for l in stat.splitlines()))
        lines.append("")
    return "\n".join(lines), data


# -------------------------------------------------------------------- handoff


def build_handoff(
    chat_id: str, old_session_id: str, new_seq: int, rule: "Rule",
    facts: Dict[str, Any], reason: str,
) -> Optional[Path]:
    """Assemble everything the new session needs. Zero model tokens (L0)."""
    try:
        out_dir = HANDOFF_DIR / chat_id
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{new_seq}.md"

        task = _api_get(f"/tasks/{chat_id}", retries=2) or {}
        history = fetch_chat_messages(
            chat_id, since=None,
            limit=HANDOFF_HISTORY_LIMIT or 100_000,
        )
        history_text, _paths = render_chat_history(history, chat_id)
        transcript = _transcript_path(rule.workdir, old_session_id)
        skeleton = _extract_skeleton(transcript)
        ws = find_workspace(rule.workdir, [old_session_id, chat_id], chat_id)
        ws_text, _ws_data = workspace_facts(ws)

        ctx_str = _fmt_int(facts.get("ctx_tokens"))
        body = f"""# Handoff — YouGile task {chat_id}

The previous claude session for this chat was retired ({reason}); its context
had grown to {ctx_str} tokens. You are its replacement and you start with no
memory of the task. Everything below was assembled mechanically from YouGile,
git and the old transcript — it is the whole state, so read it before acting.

## Task card

- Title: {task.get('title') or '(unknown)'}
- Deadline: {(task.get('deadline') or {}).get('deadline') if isinstance(task.get('deadline'), dict) else task.get('deadline')}
- Description:

{(task.get('description') or '(empty)')}

## Workspace, branches and pull requests

{ws_text}

## Full task chat ({len(history)} messages, oldest first)

```
{history_text}
```

## What the previous session did

{skeleton}

## Escape hatch

The old transcript is still on disk. If you need a detail that is not captured
above, grep it rather than guessing:

    rg -n "<what you need>" {transcript}

Old session id: `{old_session_id}` (openable locally with `claude --resume {old_session_id}`)
"""
        path.write_text(body, encoding="utf-8")
        log.info("handoff written: %s (%d bytes)", path, len(body))
        return path
    except Exception:
        log.exception("failed to build handoff for chat %s", chat_id)
        return None


def handoff_text(path: Optional[Path]) -> str:
    """The handoff, wrapped in its instructions, ready to go into the prompt.

    The file stays on disk as the artefact to inspect after the fact; what the
    agent gets is its contents, because a file it has to open is the same tokens
    plus a round-trip.
    """
    if not path:
        return ""
    try:
        body = path.read_text(encoding="utf-8")
    except OSError:
        log.exception("handoff unreadable: %s", path)
        return (
            "SESSION HANDOFF — the previous session for this task was retired, "
            "but its handoff file could not be read. Reconstruct the state from "
            "the task chat and the workspace before acting."
        )
    return HANDOFF_BLOCK.format(body=body)


def maybe_digest(path: Optional[Path], rule: "Rule") -> None:
    """L1: compress a fat handoff with a cheap model in a FRESH session.

    Deliberately not `--resume` on the outgoing session: that would re-read the
    very context we are rotating away from, at the write rate, which is the
    cost we are trying to avoid.
    """
    if path is None or HANDOFF_DIGEST == "off":
        return
    try:
        size_kb = path.stat().st_size / 1024
    except OSError:
        return
    if size_kb < HANDOFF_DIGEST_THRESHOLD_KB:
        return
    raw = path.with_suffix(".raw.md")
    prompt = (
        "Read the handoff file below and rewrite it as a tight 1-2 page briefing "
        "for an engineer taking over this task cold. Keep every branch name, PR "
        "URL, file path, decision and open question; drop repetition and chatter. "
        "Output ONLY the rewritten briefing in Markdown, no preamble.\n\n"
        f"File: {path}\n\n{path.read_text(encoding='utf-8')}"
    )
    try:
        path.replace(raw)
        p = subprocess.run(
            # --strict-mcp-config with no --mcp-config: no MCP servers at all.
            # This call rewrites a text file and has no business holding a
            # handle on the chat, and every server's schema would otherwise ride
            # along in its context for nothing.
            [CLAUDE_BIN, "--model", HANDOFF_DIGEST_MODEL, "--strict-mcp-config",
             "--session-id", str(uuid.uuid4()), "-p", prompt],
            cwd=rule.workdir, capture_output=True, text=True,
            timeout=HANDOFF_DIGEST_TIMEOUT,
        )
        out = (p.stdout or "").strip()
        if p.returncode == 0 and len(out) > 500:
            path.write_text(
                out + f"\n\n---\nUncompressed original: `{raw}`\n", encoding="utf-8")
            log.info("handoff digested: %.1f KB -> %.1f KB", size_kb, len(out) / 1024)
            return
        log.warning("handoff digest returned nothing usable (rc=%s) — keeping L0", p.returncode)
    except Exception:
        log.exception("handoff digest failed — keeping L0")
    # Any failure: put the mechanical version back.
    try:
        if not path.exists() and raw.exists():
            raw.replace(path)
    except OSError:
        log.exception("failed to restore raw handoff")


# ------------------------------------------------------------------ rendering


def render_session_stats(
    facts: Dict[str, Any], state: Dict[str, Any], note: str = "",
) -> str:
    """The block the agent appends verbatim to its ack.

    Also the only place a session change is announced. A rotation used to arrive
    as its own message moments before the agent's ack, so crossing the threshold
    cost the chat two messages that talked about the same numbers — and the
    second one silently contradicted the first by carrying on regardless. One
    turn, one message: `note` rides along here.
    """
    sv = facts["session"]
    chain = len(state.get("sessions") or []) + 1
    ctx = facts.get("ctx_tokens")
    cost = facts.get("expected_cost")
    spent = (state.get("session_totals") or {}).get("cost_usd")
    # Plain text, no markup: the agent pastes this into `text` as well as into
    # `textHtml`, and a mark would show up as a literal character there.
    line = msg("stats.session", id=sv["id"])
    if chain > 1:
        line += msg("stats.chain_position", position=sv["seq"] + 1)
    bits = []
    if ctx:
        bits.append(msg("stats.context", tokens=_fmt_int(ctx)))
    if cost is not None:
        bits.append(msg("stats.turn_cost", cost=_fmt_usd(cost)))
    if spent:
        bits.append(msg("stats.session_cost", cost=_fmt_usd(spent)))
    stats = line + ("\n" + " · ".join(bits) if bits else "")
    return f"{note}\n{stats}" if note else stats


def render_rotation_note(
    old_id: str, reason: str, facts: Dict[str, Any], totals: Dict[str, Any],
) -> str:
    """Two lines for the ack: a new session started, and what the old one cost.

    Everything the retired session's full breakdown used to say is still one
    `/sessions` away — what belongs in the chat is the decision (new session),
    the reason, and the three numbers behind it: context, spend, threshold.
    """
    why = {
        "cost": msg("rotation.reason_cost", age=_fmt_age(facts.get("idle_min"))),
        "hard-ctx": msg("rotation.reason_hard_ctx"),
        "command": msg("rotation.reason_command"),
    }.get(reason, reason)
    return "\n".join([
        msg("rotation.started", prefix=COMMAND_REPLY_PREFIX, why=why),
        msg("rotation.previous",
            id=old_id,
            tokens=_fmt_int(facts.get("ctx_tokens")),
            spent=_fmt_usd(totals.get("cost_usd")),
            expected=_fmt_usd(facts.get("expected_cost")),
            threshold=_fmt_usd(ROTATE_TURN_COST_USD)),
    ])


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# --- Message layout ----------------------------------------------------------
# YouGile renders `textHtml` and shows `text` verbatim, so every message we post
# needs both: the HTML the user reads, and a plain fallback for notifications
# and search, where markup would show up as literal characters. Both come from
# the same source — the messages file — written with four marks:
#
#     # heading         ->  <h4>
#     - item            ->  <ul><li>
#     **emphasis**      ->  <b>
#     `literal`         ->  <code>
#     [label](url)      ->  <a href="url">label</a>
#
# A blank line starts a new paragraph; single newlines inside one become <br>.
# Everything else is literal, and every value the code interpolates is escaped,
# so a branch name with an angle bracket in it cannot break the message.
#
# Deliberately NOT used for: the ack block the agent pastes into its own message
# (plain text only — it lands in `text` as well), and the quoted claude output in
# a failure report (raw output, whose backticks would be read as markup).
_MARK_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_MARK_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MARK_CODE = re.compile(r"`([^`]+)`")


def _inline_text(s: str) -> str:
    s = _MARK_LINK.sub(lambda m: f"{m.group(1)}: {m.group(2)}", s)
    s = _MARK_BOLD.sub(r"\1", s)
    return _MARK_CODE.sub(r"\1", s)


def _inline_html(s: str) -> str:
    s = _html_escape(s)
    s = _MARK_LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', s)
    s = _MARK_BOLD.sub(r"<b>\1</b>", s)
    return _MARK_CODE.sub(r"<code>\1</code>", s)


def render_message(lines: List[str]) -> Tuple[str, str]:
    """(text, textHtml) from a list of marked-up lines. "" starts a paragraph."""
    text_out: List[str] = []
    html_out: List[str] = []
    para: List[str] = []
    bullets: List[str] = []

    def flush() -> None:
        if bullets:
            html_out.append(
                "<ul>" + "".join(f"<li>{_inline_html(b)}</li>" for b in bullets) + "</ul>")
            text_out.append("\n".join("• " + _inline_text(b) for b in bullets))
            bullets.clear()
        if para:
            html_out.append(
                "<p>" + "<br>".join(_inline_html(p) for p in para) + "</p>")
            text_out.append("\n".join(_inline_text(p) for p in para))
            para.clear()

    for raw in lines:
        line = (raw or "").rstrip()
        if not line:
            flush()
        elif line.startswith("# "):
            flush()
            html_out.append(f"<h4>{_inline_html(line[2:])}</h4>")
            text_out.append(_inline_text(line[2:]))
        elif line.startswith("- "):
            if para:  # a bullet list closes the paragraph above it
                flush()
            bullets.append(line[2:])
        else:
            if bullets:
                flush()
            para.append(line)
    flush()
    return "\n\n".join(text_out), "".join(html_out)


def render_help() -> Tuple[str, str]:
    # Numbers arrive pre-formatted: a format spec inside a translatable string
    # is a landmine for whoever edits the file next.
    return render_message([
        msg("help.title", prefix=COMMAND_REPLY_PREFIX),
        msg("help.intro"),
        "",
        msg("help.status"),
        msg("help.sessions"),
        msg("help.new"),
        msg("help.keep"),
        msg("help.stop"),
        msg("help.help"),
        "",
        msg("help.mention", mention=AGENT_MENTION),
        msg("help.prices",
            price_in=f"{PRICE_IN_PER_MTOK:.0f}",
            price_out=f"{PRICE_OUT_PER_MTOK:.0f}",
            cache_write=f"{CACHE_WRITE_MULT:g}",
            cache_read=f"{CACHE_READ_MULT:g}"),
    ])


def render_status(chat_id: str, rule: "Rule") -> Tuple[str, str]:
    state = get_chat_state(chat_id)
    if not state:
        return render_message(
            [msg("status.never_ran", prefix=COMMAND_REPLY_PREFIX)])
    facts = session_facts(chat_id, state, rule)
    sv = facts["session"]
    tp = _transcript_path(rule.workdir, sv["id"])
    totals = _usage_totals(tp)
    chain = list(state.get("sessions") or [])
    chain_cost = sum((s.get("cost_usd") or 0) for s in chain) + (totals.get("cost_usd") or 0)
    last = facts.get("last_turn") or {}
    busy = chat_turn_in_flight(chat_id)

    # Four short blocks, in the order the questions get asked: what is this
    # session, what happens next, where is the work, and housekeeping. No column
    # alignment — the chat panel is narrow and wraps, which is exactly what made
    # the old <pre> dump unreadable.
    L = [msg("status.title", prefix=COMMAND_REPLY_PREFIX,
             agent=msg("status.agent_busy" if busy else "status.agent_idle")),
         "",
         msg("status.session", position=sv["seq"] + 1, total=len(chain) + 1,
             id=sv["id"])]
    if facts.get("ctx_tokens"):
        L.append(msg("status.context",
                     tokens=_fmt_count(facts["ctx_tokens"], "tokens"),
                     calls=_fmt_count(totals.get("calls", 0), "calls"),
                     age=_fmt_age(facts.get("idle_min"))))
    else:
        L.append(msg("status.context_unknown"))
    if last:
        L.append(msg("status.last_turn_cold" if last.get("cold")
                     else "status.last_turn_warm",
                     cost=_fmt_usd(last.get("cost_usd")),
                     read=_fmt_int(last.get("cache_read")),
                     write=_fmt_int(last.get("cache_creation"))))
    L.append(msg("status.spent",
                 total=_fmt_usd(totals.get("cost_usd")),
                 write=_fmt_usd(totals.get("write_usd")),
                 read=_fmt_usd(totals.get("read_usd")),
                 output=_fmt_usd(totals.get("output_usd"))))
    if chain:
        L.append(msg("status.spent_task", cost=_fmt_usd(chain_cost),
                     sessions=_fmt_count(len(chain) + 1, "sessions")))

    L += ["", msg("status.next_header")]
    exp = facts.get("expected_cost")
    if exp is None:
        L.append(msg("status.next_unknown"))
    else:
        rotate_now = (facts.get("ctx_tokens") or 0) >= ROTATE_HARD_CTX or exp > ROTATE_TURN_COST_USD
        verdict = msg("status.verdict_rotate" if rotate_now else "status.verdict_keep")
        if SESSION_ROTATE_MODE != "auto" and rotate_now:
            verdict += msg("status.verdict_advisory_only", mode=SESSION_ROTATE_MODE)
        L.append(msg("status.next_turn", cost=_fmt_usd(exp), verdict=verdict))
        cold_cost = expected_turn_cost(facts.get("ctx_tokens"), 999)
        if cold_cost and not rotate_now:
            L.append(msg("status.next_if_idle", cost=_fmt_usd(cold_cost)))
    L.append(msg("status.policy", mode=SESSION_ROTATE_MODE,
                 threshold=_fmt_usd(ROTATE_TURN_COST_USD),
                 calls=f"{calls_per_turn():.0f}",
                 ctx_cap=_fmt_int(ROTATE_HARD_CTX)))
    L.append(msg("status.cache_decay", buckets=" · ".join(
        msg("status.cache_decay_bucket",
            percent=f"{100 * miss_probability(k):.0f}",
            after=msg(f"status.cache_decay_after_{k.replace('-', '_').replace('+', 'plus')}"))
        for k in ("0-10", "10-30", "30-60", "60+"))))

    ws = find_workspace(rule.workdir, [s.get("id") for s in chain] + [sv["id"]], chat_id)
    if ws:
        L += ["", *_render_workspace(ws)]

    L += ["", msg("status.processed_up_to",
                  last_message_id=state.get("last_message_id"))]
    return render_message(L)


def _render_workspace(ws: Path) -> List[str]:
    """The workspace block of /status: one bullet per repo, PRs as links.

    The branch is named once for the whole workspace — all repos of a feature
    share it in practice, and a repo that wandered off says so itself.
    """
    try:
        data = json.loads((ws / ".feature.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [msg("status.workspace_unreadable", name=ws.name)]
    repos = data.get("repos") or {}
    branches = [r.get("branch") for r in repos.values() if r.get("branch")]
    common = branches[0] if branches and len(set(branches)) == 1 else ""
    lines = [msg("status.workspace" if common else "status.workspace_mixed",
                 name=ws.name, branch=common)]
    for name, r in repos.items():
        bits = [msg("status.repo_base", base=r.get("base") or "?")]
        if not common and r.get("branch"):
            bits.append(msg("status.repo_branch", branch=r["branch"]))
        pr = r.get("pr")
        bits.append(msg("status.repo_pr", label=_pr_label(pr), url=pr) if pr
                    else msg("status.repo_no_pr"))
        if r.get("url"):
            bits.append(msg("status.repo_preview", url=r["url"]))
        lines.append(msg("status.repo", repo=name, bits=" · ".join(bits)))
    return lines


def _pr_label(pr: str) -> str:
    """"PR 161" from a pull-request URL; the raw value if it isn't one."""
    tail = str(pr).rstrip("/").rsplit("/", 1)[-1]
    return msg("status.pr_number", number=tail) if tail.isdigit() else str(pr)


def render_sessions(chat_id: str, rule: "Rule") -> Tuple[str, str]:
    state = get_chat_state(chat_id)
    sv = session_view(chat_id, state)
    chain = list(state.get("sessions") or [])
    totals = _usage_totals(_transcript_path(rule.workdir, sv["id"]))
    spent = sum((s.get("cost_usd") or 0) for s in chain) + (totals.get("cost_usd") or 0)
    L = [msg("sessions.title", prefix=COMMAND_REPLY_PREFIX,
             count=len(chain) + 1, spent=_fmt_usd(spent)), ""]
    for s in chain:
        L.append(msg("sessions.retired", seq=s.get("seq"), id=s.get("id"),
                     calls=_fmt_count(s.get("calls", s.get("turns")), "calls"),
                     cost=_fmt_usd(s.get("cost_usd")),
                     retired_at=_fmt_when(s.get("retired_at")),
                     reason=s.get("reason")))
    L.append(msg("sessions.current", seq=sv["seq"], id=sv["id"],
                 calls=_fmt_count(totals.get("calls"), "calls"),
                 cost=_fmt_usd(totals.get("cost_usd"))))
    L += ["", msg("sessions.footer")]
    return render_message(L)


# ------------------------------------------------------------------- commands

COMMAND_RE = re.compile(
    r"^\s*(?:@\S+\s+)?/(help|status|sessions|new|keep|stop)\b\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _plain_text(payload: Any) -> str:
    msg = payload.get("payload") if isinstance(payload, dict) else None
    text = (msg or {}).get("text") if isinstance(msg, dict) else None
    return _TAG_RE.sub(" ", text or "").strip()


# `/new` and `/keep` only set what the NEXT turn should do. When one of them
# carries text beyond itself, that text is an instruction, not a reason:
# "@Agent /new доделай …" reads as "start fresh AND do this". Consuming it as a
# plain command arms the flag and drops the instruction, leaving the receiver to
# answer "принято" for a task nobody is working on — so these fall through to
# the ordinary rules and the agent runs.
COMMAND_MODIFIERS = frozenset({"new", "keep"})


def command_carries_instruction(cmd: str, arg: str) -> bool:
    return cmd in COMMAND_MODIFIERS and bool((arg or "").strip())


def _is_command_noise(text: str) -> bool:
    """True for a slash command or for one of our own replies to one."""
    if not COMMANDS_ENABLED:
        return False
    plain = _TAG_RE.sub(" ", text or "").strip()
    if COMMAND_REPLY_PREFIX and plain.startswith(COMMAND_REPLY_PREFIX):
        return True
    m = COMMAND_RE.match(plain)
    if not m:
        return False
    # A modifier that carries an instruction is a real message: filtering it out
    # of the history would hide from the agent the very thing it was asked to do.
    return not command_carries_instruction(
        m.group(1).lower(), (m.group(2) or "").strip())


def parse_command(payload: Any) -> Optional[Tuple[str, str]]:
    if not COMMANDS_ENABLED:
        return None
    event = payload.get("event") if isinstance(payload, dict) else None
    if event != "chat_message-created":
        return None
    m = COMMAND_RE.match(_plain_text(payload))
    if not m:
        return None
    return m.group(1).lower(), (m.group(2) or "").strip()


def command_sender_allowed(payload: Any) -> bool:
    allowed = set(COMMAND_ALLOWED_EMAILS)
    if not allowed:
        for r in RULES:
            allowed |= {e.lower() for e in (r.allowed_sender_emails or [])}
    if not allowed:
        return True  # no allowlist configured anywhere — same as the rules
    # Same lookup the rules use. YouGile puts fromUserId at the TOP level of a
    # chat_message-created payload, not inside `payload` — reading one fixed
    # path found nothing and silently rejected the owner's own commands.
    uid = _deep_get(payload, SENDER_KEYS)
    user = USERS_BY_ID.get(uid) or {}
    return (user.get("email") or "").lower() in allowed


def _post_command_refusal(chat_id: Optional[str], cmd: str) -> None:
    """Tell the sender their command was recognised but not theirs to run."""
    if not chat_id:
        return
    text, html = render_message(
        [msg("command.not_allowed", prefix=COMMAND_REPLY_PREFIX, command=cmd)])

    def _send() -> None:
        try:
            send_chat_message(chat_id, text, html)
        except Exception:
            log.exception("command refusal send failed chat=%s", chat_id)

    threading.Thread(target=_send, daemon=True).start()


def handle_command(cmd: str, arg: str, payload: Any) -> Dict[str, Any]:
    """Answer in-process. Never spawns an agent, never touches the sticker."""
    chat_id = extract_chat_id(payload)
    if not chat_id:
        return {"handled": False}
    rule = next((r for r in RULES if r.enabled and r.session_per_chat), None)
    if rule is None:
        rule = RULES[0] if RULES else None
    if rule is None:
        return {"handled": False}

    if cmd == "help":
        text, html = render_help()
    elif cmd == "status":
        text, html = render_status(chat_id, rule)
    elif cmd == "sessions":
        text, html = render_sessions(chat_id, rule)
    elif cmd == "new":
        update_chat_state(chat_id, pending_rotation=True, skip_rotation_once=False)
        if command_carries_instruction(cmd, arg):
            # No reply of our own: the agent is about to post its ack, and the
            # "started a new session" line rides on that. One message per turn.
            return {"handled": True, "command": cmd, "continue_to_rules": True}
        extra = msg("command.new_reason", reason=arg) if arg else ""
        text, html = render_message(
            [msg("command.new_ok", prefix=COMMAND_REPLY_PREFIX) + extra])
    elif cmd == "keep":
        update_chat_state(chat_id, skip_rotation_once=True, pending_rotation=False)
        if command_carries_instruction(cmd, arg):
            return {"handled": True, "command": cmd, "continue_to_rules": True}
        text, html = render_message(
            [msg("command.keep_ok", prefix=COMMAND_REPLY_PREFIX)])
    elif cmd == "stop":
        stopped = _interrupt_chat(chat_id)
        text, html = render_message(
            [msg("command.stop_ok" if stopped else "command.stop_idle",
                 prefix=COMMAND_REPLY_PREFIX)])
    else:
        return {"handled": False}

    threading.Thread(
        target=send_chat_message, args=(chat_id, text, html), daemon=True
    ).start()
    return {"handled": True, "command": cmd}


# ---------------------------------------------------------------- spawning

app = FastAPI(title="YouGile webhook receiver", version=APP_VERSION)


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
    # Before any turn can spawn: the config names this checkout and this
    # interpreter, so it is rewritten on every start rather than trusted.
    if write_mcp_config():
        log.info("MCP: %s -> %s", MCP_CONFIG_FILE, MCP_SERVER_SCRIPT)
    if LOOP_WATCHDOG_TIMEOUT_SECONDS > 0:
        threading.Thread(target=_loop_watchdog, daemon=True).start()
        log.info(
            "watchdog: armed (timeout=%ds, check=%ds)",
            LOOP_WATCHDOG_TIMEOUT_SECONDS, LOOP_WATCHDOG_CHECK_SECONDS,
        )
    # Turns outlive this process, so a fresh one starts by finding out what the
    # last one left behind. Off the event loop: it talks to the YouGile API.
    threading.Thread(target=reconcile_open_turns, daemon=True).start()


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


def chat_turn_in_flight(chat_id: str) -> bool:
    """True while a turn for this chat is being launched or is running.

    The on-disk companion to chat_known(): the lock is taken at the very top of
    spawn_claude, well before the state file is written, so together they cover
    the whole lifetime of a chat's session.
    """
    with _chat_locks_mutex:
        lock = _chat_busy.get(chat_id)
    return lock is not None and lock.locked()


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
        pid = proc.pid if proc is not None else None
        if pid is None:
            # A turn adopted after a restart is not our child and has no Popen
            # here — its pid is in the record, and killpg works on it all the
            # same. Without this an inherited turn could never be redirected.
            rec = read_turn(chat_id)
            candidate = rec.get("pid")
            if _pid_alive(candidate):
                pid = candidate
        if pid is None:
            return False
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


def _post_turn_failure(
    chat_id: Optional[str], headline: str, reason: str = "", footer: str = "",
) -> None:
    """Tell the chat that a turn died, and why (fire-and-forget).

    The 👍 on the trigger message is a promise that something is happening. When
    the turn can't keep that promise, this message says so — with the actual
    error, not a generic apology, because the cause is usually something only
    the user can act on (re-mention, /new, check the login).
    """
    if not (chat_id and TURN_FAILURE_REPORT):
        return
    headline = f"{TURN_FAILURE_PREFIX} {headline}".strip()
    blocks = [headline]
    html = [f"<p>{_inline_html(headline)}</p>"]
    if reason:
        blocks.append(reason)
        # Claude's own output, set apart as a quote and NEVER read as markup:
        # backticks and asterisks are ordinary characters in an error message.
        html.append(f"<pre><code>{_html_escape(reason)}</code></pre>")
    if footer:
        blocks.append(footer)
        html.append(f"<p>{_inline_html(footer)}</p>")
    text = "\n\n".join(blocks)

    def _send() -> None:
        try:
            ok = send_chat_message(chat_id, text, "".join(html))
            log.info("turn failure reported to chat=%s ok=%s", chat_id, ok)
        except Exception:
            log.exception("turn failure report failed chat=%s", chat_id)

    threading.Thread(target=_send, daemon=True).start()


def _report_dead_turn(
    chat_id: Optional[str],
    rc: int,
    reason: str,
    session_lost: bool,
    recovering: bool,
) -> None:
    """Phrase a dead turn for the chat: what died, why, and what happens next."""
    if session_lost and recovering:
        _post_turn_failure(
            chat_id,
            msg("turn_failure.session_lost", rc=rc),
            reason,
            msg("turn_failure.session_lost_footer"),
        )
    elif session_lost:
        _post_turn_failure(
            chat_id,
            msg("turn_failure.session_lost_again", rc=rc),
            reason,
            msg("turn_failure.session_lost_again_footer", mention=AGENT_MENTION),
        )
    else:
        # The tail is claude's last word, whatever it was — an error, or (if it
        # died only after answering) the end of the answer. The wording works
        # either way; guessing which one it is would be worse than saying both.
        _post_turn_failure(
            chat_id,
            msg("turn_failure.generic", rc=rc),
            reason,
            msg("turn_failure.generic_footer", mention=AGENT_MENTION),
        )


def _recover_lost_session(chat_id: str, rule: "Rule", payload: Any) -> None:
    """Retire a session claude refused to open, then replay the turn once.

    A lost session is terminal for that id, so leaving the chat pointed at it
    bricks the chat: every later mention resumes the same dead id, exits 1 in
    two seconds and answers nothing. Advancing the session chain (the same
    bookkeeping a normal rotation does, minus the handoff — there is no
    transcript left to hand off) makes the next turn bootstrap a fresh session,
    and the replay means the user's message is acted on now rather than after
    they notice and re-send it.
    """

    def _run() -> None:
        try:
            state = get_chat_state(chat_id)
            facts = session_facts(chat_id, state, rule)
            old_id = facts["session"]["id"]
            new_state = rotate_session(chat_id, state, "session-lost", facts, rule)
            new_id = new_state["session_id"]
            log.warning(
                "chat %s: session %s is gone — recovering into %s",
                chat_id, old_id, new_id,
            )
            # Re-point the feature workspace at the new session id, or the
            # replay would fail to find its branch and open a second PR.
            ws = find_workspace(rule.workdir, [old_id, chat_id], chat_id)
            if ws:
                patch_feature_json(ws, new_id, chat_id)
                update_chat_state(chat_id, workspace_path=str(ws))

            time.sleep(SESSION_RECOVERY_DELAY_SECONDS)
            if chat_turn_in_flight(chat_id):
                # Something was queued behind the dead turn and already took
                # over. It bootstraps the fresh session by itself, and it is
                # newer than what we would replay — let it have the chat.
                log.info("chat %s: newer turn took over — skipping replay", chat_id)
                return
            ok, why = spawn_claude(rule, payload, allow_session_recovery=False)
            log.info("chat %s: session-loss replay ok=%s reason=%s", chat_id, ok, why)
            if not ok:
                _post_turn_failure(
                    chat_id,
                    msg("turn_failure.replay_failed", why=why),
                    footer=msg("turn_failure.replay_failed_footer",
                               mention=AGENT_MENTION),
                )
        except Exception:
            log.exception("chat %s: session-loss recovery failed", chat_id)

    threading.Thread(target=_run, daemon=True).start()


def _mark_turn_finished(chat_id: Optional[str], task_id: Optional[str]) -> None:
    """Flip the card to "answered" now that the agent has stopped — the user's
    cue that the reply is in the chat and it's their turn.

    Skipped while another turn is already queued for this chat (an interrupt, or
    a follow-up that landed mid-turn): the agent isn't actually done, so the card
    keeps saying "working" instead of blinking "read me" and back a second later.
    Whoever skips it owes the flip to the turn that follows — see _release_chat,
    which flips it when the queued turn fails to start.
    """
    if chat_id:
        with _active_lock:
            if chat_id in _pending_turns:
                return
    set_task_marker(task_id, MARKER_ANSWERED)


def _release_chat(
    chat_id: Optional[str],
    chat_lock: Optional[threading.Lock],
    task_id: Optional[str] = None,
) -> None:
    """Release a chat's turn lock, then run any turn queued while it was busy.

    Single exit point for every turn (normal completion, spawn failure, or
    exception), so an interrupting instruction recorded in _pending_turns is
    never orphaned. The queued turn starts here — chained off the dying turn —
    which is why interrupting never blocks the webhook handler.
    """
    if chat_lock is not None:
        try:
            chat_lock.release()
        except RuntimeError:
            pass  # already released — finishing twice must not raise
    if not chat_id:
        return
    with _active_lock:
        pending = _pending_turns.pop(chat_id, None)
    if pending is None:
        return
    p_rule, p_payload = pending
    ok, why = spawn_claude(p_rule, p_payload, interrupted=True)
    if ok:
        return
    # The queued turn is the reason _mark_turn_finished left the card on
    # "working". It never started, so nothing else will ever clear it.
    set_task_marker(task_id, MARKER_ANSWERED)
    if why == "global-max-concurrent":
        _post_capacity_message(chat_id)


def finish_turn(
    rec: Dict[str, Any],
    rc: int,
    failure: Optional[str],
    *,
    adopted_dead: bool = False,
) -> None:
    """Everything the receiver owes a turn once its process is gone.

    Takes the turn's on-disk record rather than a closure, so it works both for
    a turn this process started and for one it inherited from a process that no
    longer exists. Nothing here depends on in-memory state that a restart would
    take with it: the usage figures come from claude's transcript, the watermark
    and the queued instruction from the record, the rest from the API.
    """
    chat_id = rec.get("chat_id")
    task_id = rec.get("task_id")
    session_id = rec.get("session_id")
    rule = next((r for r in RULES if r.name == rec.get("rule")), None)
    # The prompt never reached a session, whatever the reason — those messages
    # were not delivered and the watermark has to go back.
    undelivered = bool(failure and SESSION_LOST_PATTERN.search(failure))
    # But "already in use" on a session whose transcript is sitting on disk is a
    # collision, not a loss: the id is fine, this attempt just failed to claim
    # it. Rotating away would throw a live session's context out over what is
    # usually a self-inflicted double-claim.
    session_lost = undelivered and not (
        bool(session_id) and rule is not None
        and _transcript_path(rule.workdir, session_id).exists()
    )
    recovering = (
        session_lost and bool(chat_id) and bool(rule)
        and rec.get("allow_session_recovery", True)
    )
    try:
        if chat_id and undelivered:
            # Put the watermark back or those messages are lost.
            prev = rec.get("prev_watermark")
            update_chat_state(chat_id, last_message_id=prev)
            log.warning(
                "chat %s: session was gone — watermark rolled back to %s",
                chat_id, prev,
            )
        if chat_id and session_id and rule:
            _record_turn_metrics(chat_id, session_id, rule, rec.get("idle_before"))
        # A queued instruction survives a restart only if it was written down.
        pending = rec.get("pending")
        if chat_id and pending and rule is not None:
            p_rule = next(
                (r for r in RULES if r.name == pending.get("rule")), None)
            if p_rule is not None:
                with _active_lock:
                    _pending_turns.setdefault(
                        chat_id, (p_rule, pending.get("payload")))
        _mark_turn_finished(chat_id, task_id)
        if failure:
            with _active_lock:
                queued = bool(chat_id) and chat_id in _pending_turns
            # A newer instruction is already chained behind this turn (see
            # _release_chat) and answers in its place, so reporting here posts
            # the same failure twice for what the user lived as one exchange.
            # A lost session is the exception: the replacement runs into it too,
            # and this is the message that explains the rotation.
            if session_lost or not queued:
                _report_dead_turn(chat_id, rc, failure, session_lost, recovering)
        elif chat_id and TURN_REPORT_SILENT and not rec.get("killed"):
            with _active_lock:
                queued = chat_id in _pending_turns
            if not queued:
                _report_silent_turn(chat_id, rec, adopted_dead)
    except Exception:
        log.exception("chat %s: finishing turn failed", chat_id)
    finally:
        if chat_id:
            clear_turn(chat_id)
        lock = _get_chat_lock(chat_id) if chat_id else None
        _release_chat(chat_id, lock, task_id)
    if recovering and chat_id and rule is not None:
        _recover_lost_session(chat_id, rule, rec.get("payload"))


def _report_silent_turn(
    chat_id: str, rec: Dict[str, Any], adopted_dead: bool
) -> None:
    """Say something when a turn ended cleanly but the agent never spoke.

    The receiver does not write the agent's answers — the agent posts them over
    MCP — so "the process exited" and "the user got a reply" are different
    facts. When they disagree the chat otherwise shows a 👍 and then silence
    forever, which is indistinguishable from a turn still running.
    """
    replied = agent_replied_since(chat_id, rec.get("prev_watermark"))
    if replied is not False:
        return  # it answered, or we couldn't ask — either way, stay quiet
    log.warning("chat %s: turn ended without the agent posting anything", chat_id)
    _post_turn_failure(
        chat_id,
        msg("turn_failure.silent_restart" if adopted_dead
            else "turn_failure.silent"),
        footer=msg("turn_failure.silent_footer", mention=AGENT_MENTION),
    )


def _build_prompt(
    rule: Rule,
    payload,
    chat_history_text: str,
    is_first_turn: bool,
    attachment_paths: List[Path],
    session_id: Optional[str] = None,
    interrupted: bool = False,
    handoff_path: Optional[Path] = None,
    session_stats: str = "",
    dropped: int = 0,
    last_reply_plain: bool = False,
    last_reply_cdata: bool = False,
) -> str:
    """Render the prompt for one turn.

    A resumed session is already holding the standing instructions from its
    first turn, so re-sending them costs a full re-read of text the agent has
    not forgotten — and, being appended at the end, they compete for attention
    with the actual request. When the rule ships a follow-up template we use it;
    it carries the new messages, the stats block and a short reminder, and
    nothing else.
    """
    using_followup = bool(not is_first_turn and rule.prompt_template_followup)
    template = rule.prompt_template_followup if using_followup else rule.prompt_template
    rendered = template
    rendered = rendered.replace("{chat_history}", chat_history_text)
    rendered = rendered.replace("{first_turn}", "true" if is_first_turn else "false")
    rendered = rendered.replace("{formatting}", FORMATTING_BLOCK)
    rendered = rendered.replace("{language}", rule.language or "")
    rendered = rendered.replace("{session_id}", session_id or "")
    rendered = rendered.replace("{session_stats}", session_stats)
    chat_id = extract_chat_id(payload) or ""
    rendered = rendered.replace("{chat_id}", chat_id)
    # {event_json} used to interpolate the raw webhook event: ~340 tokens of
    # which half was `prevData`, a byte-for-byte copy of `payload`, wrapped
    # around a message that is already the last line of {chat_history}. Kept as
    # a placeholder so templates written against it keep working, but it now
    # renders the two facts that were ever read out of it.
    if "{event_json}" in rendered:
        inner = payload.get("payload") if isinstance(payload, dict) else None
        text = (inner or {}).get("text") or ""
        sender = _format_sender(
            payload.get("fromUserId") if isinstance(payload, dict) else None)
        rendered = rendered.replace(
            "{event_json}",
            f"  chat/task id : {chat_id}\n"
            f"  from         : {sender}\n"
            f"  text         : {text}",
        )
    rendered = rendered.replace(
        "{handoff}", handoff_text(handoff_path) if handoff_path else "")
    # A handoff must never be silently dropped just because the template
    # predates it — append it if the placeholder isn't there.
    if handoff_path and "{handoff}" not in template:
        rendered = f"{handoff_text(handoff_path)}\n\n---\n{rendered}"
    if session_stats and "{session_stats}" not in template:
        rendered = (
            f"{rendered}\n\n---\nWhen you post your ack message into the chat, "
            f"end it with this block, copied VERBATIM (do not recompute or "
            f"reword the numbers):\n\n{session_stats}\n"
        )
    # If the template doesn't reference {formatting} explicitly, append it so
    # every spawned agent still sees the rules — guarantees consistent output.
    # Not for a follow-up: the session was given the rules on its first turn and
    # its template says what to repeat, so appending the full block here would
    # undo the whole point of having a shorter one.
    if not using_followup and "{formatting}" not in template and FORMATTING_BLOCK:
        rendered = f"{rendered}\n\n---\n{FORMATTING_BLOCK}\n"
    # If template uses no placeholder for history, append it so the agent still sees it.
    if "{chat_history}" not in template and chat_history_text:
        rendered = (
            f"{rendered}\n\n"
            f"---\nRecent messages in this task chat "
            f"({'first turn — full history' if is_first_turn else 'new since your last reply'}):\n"
            f"{chat_history_text}\n"
        )
    # Never let a trimmed batch look like the whole story: the watermark has
    # already stepped over these, so nothing will bring them back.
    if dropped:
        rendered = (
            f"{rendered}\n\n"
            f"---\nNOTE: {dropped} older message(s) from this chat are not shown "
            f"above (the batch was larger than this turn carries). If you need "
            f"them, read the chat with `mcp__yougile-mcp__get_task_messages`.\n"
        )
    if attachment_paths:
        listed = "\n".join(f"- {_render_attachment_marker(p)}" for p in attachment_paths)
        rendered = (
            f"{rendered}\n\n"
            f"---\nAttachments referenced above are saved locally — open them "
            f"with your Read tool to view their contents:\n{listed}\n"
        )
    # Dead last, on every turn including follow-ups — see REPLY_CONTRACT. The
    # rest of the prompt says what to do; this says how the answer has to leave
    # the process, and it is the thing a 100k-token implement turn forgets.
    if REPLY_CONTRACT:
        contract = REPLY_CONTRACT
        if last_reply_plain and PLAINTEXT_NOTICE:
            contract = f"{PLAINTEXT_NOTICE}\n\n{contract}"
        if last_reply_cdata and CDATA_NOTICE:
            contract = f"{CDATA_NOTICE}\n\n{contract}"
        rendered = f"{rendered}\n\n---\n{contract}\n"
    if interrupted and INTERRUPT_NOTICE:
        rendered = f"{INTERRUPT_NOTICE}\n\n---\n{rendered}"
    return rendered


def _retry_cmd(cmd: List[str], rule: "Rule") -> List[str]:
    """The same command, but resuming the session instead of claiming its id.

    Attempt 1 writes the session transcript before it ever calls the API, so a
    retry after a 5xx would re-run `--session-id <id>` against an id that now
    exists on disk and die in under a second with "already in use" — burning
    every remaining attempt without a single API call, and then looking like a
    lost session to finish_turn. Once the transcript is there the only way back
    in is --resume.
    """
    try:
        i = cmd.index("--session-id")
    except ValueError:
        return cmd  # already resuming, or a rule with no per-chat session
    session_id = cmd[i + 1] if i + 1 < len(cmd) else None
    if not session_id or not _transcript_path(rule.workdir, session_id).exists():
        return cmd  # the attempt died before the session existed — claim it again
    return [*cmd[:i], "--resume", session_id, *cmd[i + 2:]]


def _spawn_subprocess(
    rule: Rule,
    cmd: List[str],
    header: str,
    on_exit,
    chat_id: Optional[str] = None,
    turn_log: Optional[Path] = None,
    attempt: int = 1,
) -> Optional[subprocess.Popen]:
    """Start a turn and arrange for `on_exit(rc, failure)` when it ends.

    claude's output goes to `turn_log`, not to a pipe. A pipe couples the turn
    to this process: when the receiver goes away the reader goes with it, and
    claude then either blocks on a full buffer or dies writing to a closed one —
    mid-turn, with nothing posted to the chat. A file has no reader to lose, so
    an orphaned turn finishes its work and posts its own reply; all this process
    loses is the bookkeeping, which the turn record lets a restart pick up.
    """
    turn_log = turn_log or (TURN_LOG_DIR / f"{chat_id or rule.name}.log")
    try:
        out_fp = turn_log.open("a", encoding="utf-8")
    except OSError:
        log.exception("rule %s: cannot open turn log %s", rule.name, turn_log)
        return None
    # The prompt stays in the shared log — that is the file worth grepping when
    # asking "what exactly did we send it?" — while the output goes to the
    # turn's own file, so one chat's answer can never be quoted into another's.
    try:
        with CLAUDE_LOG.open("a", encoding="utf-8") as shared:
            if attempt == 1:
                shared.write(header)
            else:
                shared.write(
                    f"\n--- retry attempt {attempt}/{CLAUDE_API_RETRIES} "
                    f"ts={datetime.now(timezone.utc).isoformat()} ---\n"
                )
            shared.write(f"--- output -> {turn_log} ---\n")
    except OSError:
        log.exception("rule %s: cannot write %s", rule.name, CLAUDE_LOG)

    out_fp.write(
        f"\n=== {datetime.now(timezone.utc).isoformat()} | rule={rule.name} "
        f"| chat={chat_id} | attempt={attempt} | start ===\n"
    )
    out_fp.flush()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=rule.workdir,
            stdout=out_fp,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        log.exception("rule %s: failed to spawn claude (attempt %d)", rule.name, attempt)
        out_fp.close()
        # On first-attempt spawn failure the caller cleans up (chat_lock etc.).
        # On retry there is no caller waiting — make sure on_exit still fires.
        if attempt > 1:
            try:
                on_exit(-1, f"{type(exc).__name__}: {exc}")
            except Exception:
                log.exception("rule %s: on_exit hook failed", rule.name)
        return None

    pid = proc.pid
    # The child holds its own duplicate of the fd; ours has done its job.
    out_fp.close()
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
        try:
            with turn_log.open("a", encoding="utf-8") as fp:
                fp.write(
                    f"\n--- exit rc={rc} attempt={attempt} "
                    f"ts={datetime.now(timezone.utc).isoformat()} ---\n"
                )
        except OSError:
            pass
        output = _read_turn_output(str(turn_log))
        log.info("rule %s: claude pid=%s attempt=%d rc=%s", rule.name, pid, attempt, rc)

        if (not was_interrupted and rc != 0 and attempt < CLAUDE_API_RETRIES
                and CLAUDE_RETRY_PATTERN.search(output)):
            delay = CLAUDE_RETRY_DELAYS[min(attempt - 1, len(CLAUDE_RETRY_DELAYS) - 1)]
            log.warning(
                "rule %s: transient API error (rc=%s) — retrying in %ds (attempt %d/%d)",
                rule.name, rc, delay, attempt + 1, CLAUDE_API_RETRIES,
            )

            def _delayed_retry():
                time.sleep(delay)
                # Resolved here, not above: attempt 1 may still have been
                # flushing its transcript when the retry was scheduled.
                retry_cmd = _retry_cmd(cmd, rule)
                if retry_cmd is not cmd:
                    log.info("rule %s: retry resumes session %s instead of claiming it",
                             rule.name, cmd[cmd.index("--session-id") + 1])
                p = _spawn_subprocess(rule, retry_cmd, header, on_exit, chat_id=chat_id,
                                      turn_log=turn_log, attempt=attempt + 1)
                if p is not None and chat_id:
                    write_turn(chat_id, pid=p.pid, attempt=attempt + 1)

            threading.Thread(target=_delayed_retry, daemon=True).start()
            return

        # An interrupt is our own doing and a fresh turn is already chained
        # behind it — that is not a failure worth telling the chat about, and
        # the replacement turn is the one that will answer. Recorded rather
        # than passed, so a restart mid-interrupt reads the same fact.
        if was_interrupted and chat_id:
            write_turn(chat_id, killed=True)
        failure = None if (rc == 0 or was_interrupted) else _failure_reason(rc, output)
        try:
            on_exit(rc, failure)
        except Exception:
            log.exception("rule %s: on_exit hook failed", rule.name)

    threading.Thread(target=_reap, daemon=True).start()
    return proc


def _supervise_adopted(rec: Dict[str, Any]) -> None:
    """Watch a turn this process did not start, then finish it.

    After a restart the running claude is no longer our child, so there is
    nothing to wait() on — poll the pid instead. Cheap: one signal-0 every few
    seconds for as long as the turn runs.
    """
    chat_id = rec.get("chat_id")
    pid = rec.get("pid")

    def _watch() -> None:
        while _pid_alive(pid):
            time.sleep(TURN_ADOPT_POLL_SECONDS)
        log.info("chat %s: adopted turn pid=%s has exited", chat_id, pid)
        with _active_lock:
            was_interrupted = pid in _interrupted_pids
            _interrupted_pids.discard(pid)
        if was_interrupted and chat_id:
            write_turn(chat_id, killed=True)
        current = read_turn(chat_id) or rec
        if was_interrupted:
            finish_turn(current, 0, None)
            return
        output = _read_turn_output(current.get("log_path"))
        # rc is unknowable for a process we never owned. Treat a turn that left
        # a recognisable error in its log as failed and everything else as a
        # clean finish — finish_turn's silent-turn check catches the rest.
        failure = None
        for pattern in (SESSION_LOST_PATTERN, CLAUDE_FATAL_PATTERN):
            if pattern.search(output):
                failure = _failure_reason(-1, output)
                break
        finish_turn(current, -1 if failure else 0, failure)

    threading.Thread(target=_watch, daemon=True).start()


def reconcile_open_turns() -> None:
    """Pick up turns left running or unfinished by a previous process.

    Runs once at startup. A turn whose process is still alive is re-supervised
    and its chat marked busy again — without this the receiver believes the chat
    is idle and will happily --resume a session another process is holding. A
    turn whose process is gone is finished right here: watermark, metrics, card,
    queued instruction, and a word to the chat if the agent never got one out.
    """
    records = list_open_turns()
    if not records:
        return
    live = dead = 0
    for rec in records:
        chat_id = rec.get("chat_id")
        if not chat_id:
            continue
        if _pid_alive(rec.get("pid")):
            live += 1
            _get_chat_lock(chat_id).acquire(blocking=False)  # re-arm "busy"
            log.info(
                "chat %s: adopting turn pid=%s started %s",
                chat_id, rec.get("pid"), rec.get("started_at"),
            )
            set_task_marker(rec.get("task_id"), MARKER_WORKING)
            _supervise_adopted(rec)
        else:
            dead += 1
            log.warning(
                "chat %s: turn pid=%s died unsupervised — finishing it now",
                chat_id, rec.get("pid"),
            )
            output = _read_turn_output(rec.get("log_path"))
            failure = _failure_reason(-1, output) if output else None
            finish_turn(rec, -1, failure, adopted_dead=True)
    log.info("reconcile: %d turn(s) adopted, %d finished after the fact", live, dead)



def _failure_reason(rc: int, output: str) -> str:
    """Why a turn died, in claude's own words.

    A recognised startup error if there is one (those are printed first and are
    the whole story), otherwise the tail of whatever was printed last. Never
    empty: the caller uses it as the reason line of a chat message, and "no
    output at all" is itself the diagnosis.
    """
    text = _ANSI_RE.sub("", output or "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for pattern in (SESSION_LOST_PATTERN, CLAUDE_FATAL_PATTERN, CLAUDE_RETRY_PATTERN):
        for line in lines:
            if pattern.search(line):
                return line[:TURN_FAILURE_QUOTE_CHARS]
    if not lines:
        return msg("turn_failure.no_output", rc=rc)
    tail = "\n".join(lines[-6:])
    return tail[-TURN_FAILURE_QUOTE_CHARS:]


def spawn_claude(
    rule: Rule,
    payload,
    interrupted: bool = False,
    allow_session_recovery: bool = True,
) -> Tuple[bool, str]:
    chat_id = extract_chat_id(payload) if rule.session_per_chat else None
    # The task whose checkbox tracks this turn. Same UUID as the chat, but
    # resolved independently of session_per_chat so the status flag works for
    # every rule.
    task_id = extract_chat_id(payload)
    chat_lock: Optional[threading.Lock] = _get_chat_lock(chat_id) if chat_id else None

    if chat_lock is not None and not chat_lock.acquire(blocking=False):
        if rule.skip_if_chat_known:
            # Auto-kickoff rule (task moved to "In progress") vs. a live turn:
            # a running turn IS the "chat already has an LLM session" this rule
            # bows out for — the human is talking to the agent right now. Never
            # queue it: a queued kickoff would simply be postponed until that
            # conversation ends and then fire as a stale auto-research run
            # minutes later. Drop it for good. This also swallows YouGile's
            # duplicate task-moved deliveries, which otherwise pile up behind
            # the first one and re-run the kickoff.
            log.info(
                "rule %s: chat %s has a live turn — dropping auto-kickoff",
                rule.name, chat_id,
            )
            return False, "chat-turn-in-flight"
        # A turn is already running for this chat. Don't drop the new message —
        # queue it and interrupt the in-flight turn; the queued turn starts the
        # instant the current one dies (chained from _release_chat), resuming the
        # same session so the agent sees the new instruction and can redirect.
        with _active_lock:
            _pending_turns[chat_id] = (rule, payload)
        # Also on disk: a receiver restart between the kill and the replacement
        # would otherwise drop the instruction the user is waiting on.
        if read_turn(chat_id):
            write_turn(chat_id, pending={"rule": rule.name, "payload": payload})
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

    # Last word on skip_if_chat_known, now that we hold the chat lock: the
    # session may have been created while this event travelled from
    # rule_matches to here (the state file is written mid-spawn by the turn
    # that owned the lock), and a turn queued before this guard existed
    # re-enters spawn_claude here long after it matched.
    if chat_id and rule.skip_if_chat_known and chat_known(chat_id):
        log.info(
            "rule %s: chat %s already has a session — dropping auto-kickoff",
            rule.name, chat_id,
        )
        _release_chat(chat_id, chat_lock)
        return False, "chat-already-has-llm-session"

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
            # A chat owns a CHAIN of claude sessions. Link 0 keeps the chat id
            # (so every pre-rotation chat carries on with no migration); later
            # links are uuid5-derived, deterministic and needing no lookup
            # table. Whether we resume or start fresh is answered by
            # `session_bootstrapped` in the chat state — the existence of the
            # state file still means only "we have run for this chat before",
            # which is what skip_if_chat_known relies on.
            chat_state = get_chat_state(chat_id)
            last_msg_id = chat_state.get("last_message_id")

            rotate, rotate_reason, facts = should_rotate(
                chat_id, chat_state, rule, interrupted)
            handoff_path: Optional[Path] = None
            session_note = ""
            if rotate:
                old_id = facts["session"]["id"]
                old_totals = _usage_totals(_transcript_path(rule.workdir, old_id))
                chat_state = rotate_session(
                    chat_id, chat_state, rotate_reason, facts, rule)
                new_id = chat_state["session_id"]
                log.info(
                    "chat %s: rotating session %s -> %s (%s, ctx=%s, est=%.2f)",
                    chat_id, old_id, new_id, rotate_reason,
                    facts.get("ctx_tokens"), facts.get("expected_cost") or 0.0,
                )
                handoff_path = build_handoff(
                    chat_id, old_id, chat_state["session_seq"], rule,
                    facts, rotate_reason,
                )
                maybe_digest(handoff_path, rule)
                ws = find_workspace(rule.workdir, [old_id, chat_id], chat_id)
                if ws:
                    patch_feature_json(ws, new_id, chat_id)
                    update_chat_state(chat_id, workspace_path=str(ws))
                # Not a message of its own: it rides on the ack the agent is
                # about to post, so the chat sees one message per turn.
                session_note = render_rotation_note(
                    old_id, rotate_reason, facts, old_totals)
                facts = session_facts(chat_id, chat_state, rule)
            elif rotate_reason.startswith("advisory:"):
                session_note = render_rotation_advice(chat_id, facts)
            elif rotate_reason == "kept":
                update_chat_state(chat_id, skip_rotation_once=False)

            sv = session_view(chat_id, chat_state)
            session_id = sv["id"]
            # A state file can claim a session that never made it to disk (spawn
            # died mid-launch). Resuming that fails hard, so fall back to
            # creating it — the transcript is the ground truth.
            bootstrapped = sv["bootstrapped"] and _transcript_path(
                rule.workdir, session_id).exists()

            if not bootstrapped:
                is_first_turn = True
                history = fetch_chat_messages(
                    chat_id, since=None,
                    limit=(HANDOFF_INLINE_MESSAGES if handoff_path
                           else CHAT_HISTORY_FIRST_TIME_LIMIT),
                )
                dropped = 0
                session_flag = ["--session-id", session_id]
                # Mark the session as live BEFORE spawn so a crash mid-launch
                # doesn't make us double-fire --session-id on retry.
                update_chat_state(
                    chat_id,
                    first_seen_at=chat_state.get("first_seen_at")
                    or datetime.now(timezone.utc).isoformat(),
                    session_seq=sv["seq"],
                    session_id=session_id,
                    session_bootstrapped=True,
                )
            else:
                is_first_turn = False
                # Uncapped, so an oversized batch can be reported rather than
                # silently trimmed: the watermark moves past everything fetched,
                # so anything dropped here is dropped for good.
                history = fetch_chat_messages(chat_id, since=last_msg_id, limit=0)
                dropped = max(0, len(history) - CHAT_HISTORY_DELTA_LIMIT)
                if dropped:
                    history = history[-CHAT_HISTORY_DELTA_LIMIT:]
                    log.warning(
                        "chat %s: %d message(s) older than the last %d are being "
                        "skipped in this turn's prompt", chat_id, dropped,
                        CHAT_HISTORY_DELTA_LIMIT,
                    )
                session_flag = ["--resume", session_id]

            # The webhook can outrun the messages API, so the very message that
            # fired this turn may be missing from the fetch above.
            history = merge_trigger_message(history, payload, last_msg_id)

            # Stats are opt-out; a session change is not. If ACK_STATS is off,
            # the note still has to reach the chat — the user needs to know the
            # conversation moved to a new session.
            session_stats = (
                render_session_stats(facts, chat_state, note=session_note)
                if ACK_STATS else session_note)
            # A resumed session wrote its own replies minutes ago and still has
            # them; only the first turn of a session needs them read back.
            history_text, attachment_paths = render_chat_history(
                history, chat_id, exclude_agent=not is_first_turn)
            if handoff_path:
                # The handoff already carries the whole chat verbatim. Sending
                # it again here is how the same 16 KB used to arrive twice in
                # one prompt.
                history_text = (
                    "(the full chat is in the handoff above — only the newest "
                    "message is repeated here)\n"
                    + history_text.rsplit("\n", 1)[-1]
                )
            # Over EVERYTHING fetched, not just what was rendered: our own posts
            # and slash commands are filtered out of the prompt but must still
            # be stepped over, or every later turn re-fetches them forever.
            newest_msg_id = max(
                [int(m["id"]) for m in history if m.get("id")] + [last_msg_id or 0]
            )
        else:
            session_id = None
            history_text = ""
            is_first_turn = True
            newest_msg_id = None
            last_msg_id = None
            dropped = 0
            session_flag = []
            handoff_path = None
            session_stats = ""
            attachment_paths = []

        # Attachments on the trigger message itself, in case it arrived too
        # early to be in the history fetch above.
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
            handoff_path=handoff_path, session_stats=session_stats,
            dropped=dropped,
            last_reply_plain=bool(chat_state.get("last_reply_plain")),
            last_reply_cdata=bool(chat_state.get("last_reply_cdata")),
        )
        spawn_args = [*effort_args(rule.extra_args), *mcp_args(),
                      *rule.extra_args, *session_flag]
        cmd = [CLAUDE_BIN, *spawn_args, "-p", prompt]

        ts = datetime.now(timezone.utc).isoformat()
        header = (
            f"\n\n=== {ts} | rule={rule.name} | chat={chat_id} | session={session_id} "
            f"| first_turn={is_first_turn} | spawn ===\n"
            f"cwd: {rule.workdir}\nbin: {CLAUDE_BIN}\nargs: {spawn_args}\n"
            f"--- prompt ---\n{prompt}\n"
        )

        # How long the chat had been idle when this turn started — the input to
        # the cache-survival statistic, recorded once the turn's usage lands.
        idle_before = facts.get("idle_min") if chat_id else None
        turn_log = TURN_LOG_DIR / (
            f"{(chat_id or rule.name)}-{ts.replace(':', '').replace('-', '')[:15]}.log"
        )

        # What the record will say. Kept here too because a turn can die faster
        # than the record gets written (`--resume` on a dead session exits in
        # about two seconds, but a bad binary exits in milliseconds), and
        # finish_turn still needs the watermark to roll back.
        base_rec: Dict[str, Any] = {
            "chat_id": chat_id, "task_id": task_id, "session_id": session_id,
            "rule": rule.name, "idle_before": idle_before,
            "prev_watermark": last_msg_id if chat_id else None,
            "watermark": newest_msg_id, "log_path": str(turn_log),
            "allow_session_recovery": allow_session_recovery, "payload": payload,
        }

        def on_exit(rc: int, failure: Optional[str] = None) -> None:
            # `failure` is claude's own last word, or None when the turn ended
            # normally / we killed it ourselves to run a newer instruction.
            # Everything else comes from the record, so a restart can finish
            # this same turn without any of the state captured here; the
            # closure only fills in what the record didn't get to say.
            rec = dict(base_rec)
            rec.update(read_turn(chat_id) if chat_id else {})
            finish_turn(rec, rc, failure)

        _reap_turn_logs()
        proc = _spawn_subprocess(
            rule, cmd, header, on_exit, chat_id=chat_id, turn_log=turn_log)
        if proc is None:
            _release_chat(chat_id, chat_lock, task_id)
            return False, "spawn-failed"

        if chat_id:
            # The process is live, so `claude -p` has the prompt and has written
            # those messages into the session transcript — they are delivered
            # even if the turn dies next second, because --resume will find them
            # there. That is what makes it safe to advance the watermark now
            # instead of at exit: waiting until exit is how a turn that never
            # gets to exit (a restart, a kill) makes the next one re-serve
            # messages the agent has already answered.
            write_turn(
                chat_id,
                task_id=task_id,
                session_id=session_id,
                rule=rule.name,
                pid=proc.pid,
                attempt=1,
                started_at=ts,
                log_path=str(turn_log),
                prev_watermark=last_msg_id,
                watermark=newest_msg_id,
                idle_before=idle_before,
                interrupted=interrupted,
                allow_session_recovery=allow_session_recovery,
                payload=payload,
                pending=None,
            )
            if newest_msg_id:
                update_chat_state(chat_id, last_message_id=newest_msg_id)

        # The turn is live — mark the card "working" so the board shows the
        # agent is on it and the user knows to wait. finish_turn flips it to
        # "answered". Deliberately after the spawn: a launch that never happened
        # must not leave the card claiming a running agent.
        set_task_marker(task_id, MARKER_WORKING)


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
        "version": APP_VERSION,
        "loop_heartbeat_age_s": round(time.monotonic() - _loop_heartbeat, 1),
        "claude_bin": CLAUDE_BIN,
        "claude_effort": CLAUDE_EFFORT or "(settings.json)",
        "default_workdir": CLAUDE_DEFAULT_WORKDIR,
        "active_claude_procs": len(_active_procs),
        # Armed once known: without it, a reply to the agent can't be told from
        # a reply to a colleague and falls back to needing a literal mention.
        "agent_user_id": agent_user_id(),
        "status_sticker": _sticker_snapshot(),
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

    # Drop re-deliveries FIRST, before anything with a side effect. YouGile
    # re-sends a message it considers slow-acked, and a duplicate that reached
    # spawn_claude would interrupt the very turn the first delivery started —
    # fatally so while that turn is still creating the session. Screening here
    # (rather than after rule matching) also keeps the reply lookup below from
    # firing several times for one message; unmatched messages now occupy the
    # dedup LRU too, which it has ample room for.
    duplicate = is_duplicate_delivery(payload)
    replied_to_agent = False
    if not duplicate:
        # A reply to the agent's own message is a mention in every sense except
        # the literal text — mark it before the rules look at the payload.
        learn_agent_user_id(payload)
        # Cheap and on this path only: the agent's own replies come back as
        # deliveries too, and this is where we notice one that went out
        # unformatted (no API call, no rule matching involved).
        note_reply_formatting(payload)
        replied_to_agent = await asyncio.to_thread(annotate_agent_reply, payload)

    # Slash commands are answered by the receiver itself — no agent spawn, no
    # tokens, no sticker change. Checked before rule matching so a bare
    # "/status" can never fall through to an @Agent rule.
    if not duplicate:
        cmd = parse_command(payload)
        if cmd:
            if not command_sender_allowed(payload):
                # Say so rather than dropping it. A command that vanishes is
                # indistinguishable from a receiver that is down, and this is
                # exactly how a bad sender lookup stayed invisible for weeks.
                sender = _deep_get(payload, SENDER_KEYS)
                log.info(
                    "command /%s from a non-allowlisted sender %s — refused",
                    cmd[0], sender,
                )
                _post_command_refusal(extract_chat_id(payload), cmd[0])
                return {"ok": True, "command": cmd[0], "handled": False,
                        "reason": "sender-not-allowed"}
            result = await asyncio.to_thread(handle_command, cmd[0], cmd[1], payload)
            log.info(
                "command /%s handled=%s continue=%s", cmd[0],
                result.get("handled"), result.get("continue_to_rules", False),
            )
            if not result.get("continue_to_rules"):
                return {"ok": True, **result}
            # The command only set state for this turn; the rest of the message
            # is the instruction, so carry on into rule matching.

    rule, reasons = find_matching_rule(payload)
    fired_rule: Optional[str] = None
    spawn_reason: Optional[str] = None
    if duplicate:
        # The first delivery already reacted and spawned — this one is a no-op.
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
        elif why in ("spawn-failed", "exception"):
            # The turn never got off the ground (claude binary missing, history
            # fetch blew up, …). The trigger message is already 👍'd, so say so
            # rather than leaving the chat waiting for an agent that never ran.
            _post_turn_failure(
                extract_chat_id(payload),
                msg("turn_failure.not_started", why=why),
                footer=msg("turn_failure.not_started_footer",
                           mention=AGENT_MENTION),
            )

    event = isinstance(payload, dict) and payload.get("event")
    log.info(
        "event=%s fired=%s spawn_reason=%s reply_to_agent=%s match_reasons=%s",
        event, fired_rule, spawn_reason, replied_to_agent, reasons,
    )
    return {
        "ok": True,
        "fired_rule": fired_rule,
        "spawn_reason": spawn_reason,
        "reply_to_agent": replied_to_agent,
        "match_reasons": reasons,
    }
