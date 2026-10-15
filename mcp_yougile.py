#!/usr/bin/env python3
"""The YouGile MCP server this service hands to the agent it spawns.

Why ours and not the upstream one: the reply channel is the product. A turn can
research perfectly, open the right PRs and still fail the user if the message
lands as a wall of `**` and `#` characters — and it did, in about half of all
replies, because the schema the agent reads at call time described `textHtml`
as "optional, rarely needed" while our prompt called it mandatory. A tool schema
sits closer to the call than any system prompt, and it won.

Two things follow from owning the server:

  * the schema tells the truth about the channel, and
  * `chatmd.normalize_message` runs on the way OUT, so a dropped `textHtml` is
    repaired instead of reaching the user. The agent never sees its own messages
    rendered, so it cannot learn its way out of this — the fix belongs here,
    where the message actually leaves.

It also makes the deployment reproducible: server, schema and API key all live
in this repo and are passed to `claude --mcp-config`, instead of a hand-installed
clone registered in ~/.claude.json on one machine.

Speaks MCP over stdio: newline-delimited JSON-RPC on stdin/stdout. STDOUT IS THE
PROTOCOL — every diagnostic goes to logs/mcp.log or stderr, never to print().

Only the five tools the agent actually calls are exposed. The upstream server
carries ~30; each one's schema is re-sent on every turn, and the boards/projects/
users half was never used. Add one here when a prompt starts needing it.
"""

import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import agent_backends
import reply_delivery
import html

import chatmd  # noqa: E402  (after sys.path, so a bare `python mcp_yougile.py` works)

SERVER_NAME = "yougile-mcp"
SERVER_VERSION = "1.0.0"
# Echoed back only when the client asks for a version we know; otherwise we
# answer with this one and let the client decide.
DEFAULT_PROTOCOL = "2025-06-18"
KNOWN_PROTOCOLS = {"2024-11-05", "2025-03-26", DEFAULT_PROTOCOL}

LOG_FILE = ROOT / "logs" / "mcp.log"


def load_dotenv(path: Path) -> None:
    """Same .env the receiver reads. Inherited environment usually already has
    the key — this is for running the server by hand to debug it."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv(ROOT / ".env")

# The agent posts AS this key's user, which need not be the account the
# receiver reads history and stickers with — set YOUGILE_AGENT_API_KEY when the
# agent has its own bot user. Same key for both is the common case.
API_KEY = (os.environ.get("YOUGILE_AGENT_API_KEY", "").strip()
           or os.environ.get("YOUGILE_API_KEY", "").strip())
API_BASE = os.environ.get("YOUGILE_API_BASE", "https://yougile.com/api-v2").rstrip("/")
API_TIMEOUT = int(os.environ.get("YOUGILE_API_TIMEOUT", "30"))
API_RETRIES = int(os.environ.get("YOUGILE_MCP_API_RETRIES", "3"))
# Under this an agent message is an ack or a one-liner, where plain text is the
# right answer. Kept in step with the receiver's own threshold.
PLAIN_MIN_CHARS = int(os.environ.get("PLAIN_REPLY_MIN_CHARS", "400"))


def log(message: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now(timezone.utc).isoformat()} {message}\n")
    except OSError:
        pass


# ----------------------------------------------------------------- YouGile API

class ApiError(Exception):
    """A call that will not come good by being repeated."""


def api(method: str, path: str, body: Optional[dict] = None) -> Any:
    """Call YouGile, retrying only what a retry can fix.

    4xx (except 429) is a permanent client error — a stale id, a bad key — and
    returning it to the agent at once is more useful than spinning on it.
    """
    if not API_KEY:
        raise ApiError("YOUGILE_API_KEY is not set for the MCP server")
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bearer {API_KEY}"}
    if data is not None:
        headers["Content-Type"] = "application/json"

    delay = 2.0
    for attempt in range(1, API_RETRIES + 1):
        request = urllib.request.Request(
            f"{API_BASE}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            if exc.code != 429 and 400 <= exc.code < 500:
                detail = exc.read().decode("utf-8", "replace")[:400]
                raise ApiError(f"HTTP {exc.code} on {method} {path}: {detail}") from exc
            reason: Any = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
        except json.JSONDecodeError as exc:
            raise ApiError(f"malformed JSON from {method} {path}") from exc

        if attempt == API_RETRIES:
            raise ApiError(f"{method} {path} failed after {attempt} attempts ({reason})")
        log(f"api {method} {path}: transient {reason} — retry {attempt} in {delay:.0f}s")
        time.sleep(delay)
        delay *= 2
    raise ApiError("unreachable")


def upload_file(file_path: Path) -> str:
    """Upload a local file, returning the URL YouGile references it by."""
    payload = file_path.read_bytes()
    boundary = f"----yougile{uuid.uuid4().hex}"
    mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"\r\n'.encode(),
        f"Content-Type: {mime}\r\n\r\n".encode(),
        payload,
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    request = urllib.request.Request(
        f"{API_BASE}/upload-file",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=max(API_TIMEOUT, 120)) as response:
            result = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise ApiError(f"upload failed: HTTP {exc.code} {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ApiError(f"upload failed: {exc}") from exc
    url = result.get("url") or result.get("fileUrl")
    if not url:
        raise ApiError(f"upload returned no URL: {json.dumps(result)[:200]}")
    return url


# ----------------------------------------------------------------------- tools

def tool_send_task_message(args: Dict[str, Any]) -> Dict[str, Any]:
    task_id = str(args.get("taskId") or "").strip()
    if not task_id:
        raise ApiError("taskId is required")
    text, text_html, notes = chatmd.normalize_message(
        args.get("text"), args.get("textHtml"), plain_min_chars=PLAIN_MIN_CHARS)
    if not (text or text_html):
        raise ApiError("refusing to post an empty message")

    body: Dict[str, Any] = {"text": text, "textHtml": text_html or ""}
    label = args.get("label")
    if label:
        body["label"] = str(label)
    if reply_delivery.queue(ROOT / os.environ.get("STATE_DIR", "state"), task_id, body):
        return {"ok": True, "queued": True,
                "reminder": "Final reply saved. End your turn now; the receiver adds measured statistics and delivers it once."}
    model = agent_backends.reply_label(ROOT / os.environ.get("STATE_DIR", "state"), task_id)
    if model:
        body["text"] = f"Модель: {model}\n\n{text}"
        if text_html:
            body["textHtml"] = f"<p>Модель: {html.escape(model)}</p>{text_html}"
    result = api("POST", f"/chats/{task_id}/messages", body)

    # Logged for every message, not just repaired ones: "the server found
    # nothing to fix" and "the server never ran" look identical from the chat,
    # and the ratio of repaired to clean is the only measure of whether the
    # prompt is still doing its half of the work.
    log(f"send {task_id} text={len(text)} html={len(text_html)}"
        + (" | repaired: " + "; ".join(notes) if notes else ""))
    # The repair is reported back in the tool result because it is the only
    # feedback this channel has: the agent cannot see its own rendered messages,
    # so a silent fix would be repeated forever.
    out: Dict[str, Any] = {"ok": True, "messageId": (result or {}).get("id")}
    if notes:
        out["repaired"] = notes
        out["reminder"] = (
            "Your message was posted, but it had to be repaired first. Write "
            "`textHtml` yourself next time — you control the layout, this "
            "fallback only guesses at it."
        )
    return out


def tool_send_task_file(args: Dict[str, Any]) -> Dict[str, Any]:
    task_id = str(args.get("taskId") or "").strip()
    raw_path = str(args.get("filePath") or "").strip()
    if not (task_id and raw_path):
        raise ApiError("taskId and filePath are required")
    path = Path(raw_path).expanduser()
    if not path.is_file():
        raise ApiError(f"no such file: {path}")

    url = upload_file(path)
    caption = str(args.get("caption") or "").strip()
    # The marker is what YouGile turns back into an attachment while rendering
    # `text`; a message carrying one is posted without HTML on purpose.
    marker = f"/root/#file:{url}"
    text = f"{caption}\n{marker}" if caption else marker
    result = api("POST", f"/chats/{task_id}/messages", {"text": text, "textHtml": ""})
    return {"ok": True, "fileUrl": url, "messageId": (result or {}).get("id")}


def tool_get_task_messages(args: Dict[str, Any]) -> Any:
    task_id = str(args.get("taskId") or "").strip()
    if not task_id:
        raise ApiError("taskId is required")
    query = []
    for key in ("limit", "offset"):
        if args.get(key) is not None:
            query.append(f"{key}={int(args[key])}")
    suffix = ("?" + "&".join(query)) if query else ""
    return api("GET", f"/chats/{task_id}/messages{suffix}")


def tool_get_task(args: Dict[str, Any]) -> Any:
    task_id = str(args.get("id") or "").strip()
    if not task_id:
        raise ApiError("id is required")
    return api("GET", f"/tasks/{task_id}")


def _query(args: Dict[str, Any], *keys: str) -> str:
    parts = [f"{key}={urllib.parse.quote(str(args[key]))}"
             for key in keys if args.get(key) is not None]
    return ("?" + "&".join(parts)) if parts else ""


def tool_get_boards(args: Dict[str, Any]) -> Any:
    return api("GET", "/boards" + _query(args, "projectId", "title", "limit", "offset"))


def tool_get_columns(args: Dict[str, Any]) -> Any:
    if not args.get("boardId"):
        raise ApiError("boardId is required")
    return api("GET", "/columns" + _query(args, "boardId", "title", "limit", "offset"))


def tool_create_task(args: Dict[str, Any]) -> Any:
    title = str(args.get("title") or "").strip()
    column_id = str(args.get("columnId") or "").strip()
    if not (title and column_id):
        raise ApiError("title and columnId are required")
    body: Dict[str, Any] = {"title": title, "columnId": column_id}
    for key in ("description", "assigned", "stickers", "deadline", "color"):
        if args.get(key) is not None:
            body[key] = args[key]
    return api("POST", "/tasks", body)


_UPDATABLE = ("title", "description", "columnId", "assigned", "completed",
              "archived", "stickers", "deadline", "timeTracking", "color")


def tool_update_task(args: Dict[str, Any]) -> Any:
    task_id = str(args.get("id") or "").strip()
    if not task_id:
        raise ApiError("id is required")
    body = {key: args[key] for key in _UPDATABLE if key in args and args[key] is not None}
    if not body:
        raise ApiError("nothing to update — pass at least one field")
    return api("PUT", f"/tasks/{task_id}", body)


_STRING = {"type": "string"}

TOOLS: Tuple[Tuple[str, str, dict, Callable[[Dict[str, Any]], Any]], ...] = (
    (
        "send_task_message",
        # Read by the agent immediately before it posts — the last word on the
        # channel, and the reason replies used to go out unformatted.
        "Submit ONE final answer or necessary question, then end your turn. No ack, "
        "progress, session notices or statistics. In a managed turn this replaces "
        "the pending draft; queued:true is success, not a reason to retry. The "
        "receiver delivers it with measured statistics after the turn ends. "
        "This is the ONLY way to reach the "
        "user: nothing you print in your own output is ever shown to them.\n"
        "YouGile renders `textHtml` and does NOT render markdown. Anything "
        "longer than one short sentence MUST set `textHtml` — a report sent as "
        "`text` alone reaches the user as raw `**`, `#` and `-` characters, "
        "however good its content is.",
        {
            "type": "object",
            "properties": {
                "taskId": {"type": "string",
                           "description": "UUID of the task whose chat to post into."},
                "text": {
                    "type": "string",
                    "description": (
                        "Plain-prose fallback, shown in notifications, search and "
                        "clients that cannot render HTML. NEVER markdown — it is "
                        "printed verbatim, markers and all."),
                },
                "textHtml": {
                    "type": "string",
                    "description": (
                        "What the user actually sees. Required for anything longer "
                        "than one sentence. Use <p>, <h4> (never <h1>/<h2>), "
                        "<ul>/<li>, <b>, <code>, <pre><code>, <a href> around every "
                        "URL. No markdown characters."),
                },
                "label": {"type": "string", "description": "Quick-link label (rare)."},
            },
            "required": ["taskId", "text"],
        },
        tool_send_task_message,
    ),
    (
        "send_task_file",
        "Upload a local file and post it into a task's chat as an attachment. "
        "Use for screenshots and artefacts — not for text, which belongs in "
        "send_task_message.",
        {
            "type": "object",
            "properties": {
                "taskId": _STRING | {"description": "UUID of the task."},
                "filePath": _STRING | {"description": "Absolute path of the local file."},
                "caption": _STRING | {"description": "Optional line above the file."},
            },
            "required": ["taskId", "filePath"],
        },
        tool_send_task_file,
    ),
    (
        "get_task_messages",
        "Read a task chat's messages, newest first.",
        {
            "type": "object",
            "properties": {
                "taskId": _STRING | {"description": "UUID of the task."},
                "limit": {"type": "number", "description": "How many to return."},
                "offset": {"type": "number", "description": "Pagination offset."},
            },
            "required": ["taskId"],
        },
        tool_get_task_messages,
    ),
    (
        "get_task",
        "Get one task by UUID or by its human code (e.g. ABC-123).",
        {
            "type": "object",
            "properties": {"id": _STRING | {"description": "Task UUID or code."}},
            "required": ["id"],
        },
        tool_get_task,
    ),
    (
        "get_boards",
        "List boards, optionally filtered by project.",
        {
            "type": "object",
            "properties": {
                "projectId": _STRING | {"description": "Only boards in this project."},
                "title": _STRING | {"description": "Filter by title (partial match)."},
                "limit": {"type": "number"},
                "offset": {"type": "number"},
            },
        },
        tool_get_boards,
    ),
    (
        "get_columns",
        "List a board's columns — how to find the columnId `update_task` needs "
        "to move a task.",
        {
            "type": "object",
            "properties": {
                "boardId": _STRING | {"description": "UUID of the board."},
                "title": _STRING | {"description": "Filter by title (partial match)."},
                "limit": {"type": "number"},
                "offset": {"type": "number"},
            },
            "required": ["boardId"],
        },
        tool_get_columns,
    ),
    (
        "create_task",
        "Create a task in a column.",
        {
            "type": "object",
            "properties": {
                "title": _STRING | {"description": "Task title."},
                "columnId": _STRING | {"description": "Column to create it in."},
                "description": _STRING,
                "assigned": {"type": "array", "items": {"type": "string"},
                             "description": "User IDs to assign."},
                "stickers": {"type": "object",
                             "additionalProperties": {"type": "string"}},
                "deadline": {"type": "object"},
                "color": _STRING,
            },
            "required": ["title", "columnId"],
        },
        tool_create_task,
    ),
    (
        "update_task",
        "Update a task: move it between columns, retitle it, set stickers, "
        "complete or archive it. Only the fields you pass are changed.",
        {
            "type": "object",
            "properties": {
                "id": _STRING | {"description": "Task UUID."},
                "title": _STRING,
                "description": _STRING | {"description": (
                    "Replaces the task's description. Only when the user "
                    "explicitly asks — reports and findings go to the chat."
                )},
                "columnId": _STRING | {"description": "Column to move the task to."},
                "assigned": {"type": "array", "items": {"type": "string"},
                             "description": "User IDs to assign."},
                "completed": {"type": "boolean"},
                "archived": {"type": "boolean"},
                "stickers": {"type": "object",
                             "description": "sticker id -> state id; '-' clears one.",
                             "additionalProperties": {"type": "string"}},
                "deadline": {"type": "object", "description": "YouGile deadline object."},
                "timeTracking": {"type": "object"},
                "color": _STRING,
            },
            "required": ["id"],
        },
        tool_update_task,
    ),
)

HANDLERS = {name: handler for name, _desc, _schema, handler in TOOLS}
# Upstream exposed the same endpoint under two names. Not advertised — it costs
# a slot in every turn's context for nothing — but still answered, because a
# session resumed from before this server existed has the old name in its
# history and would otherwise get "unknown tool" mid-reply.
HANDLERS["get_task_chat"] = tool_get_task_messages
TOOL_LIST = [
    {"name": name, "description": desc, "inputSchema": schema}
    for name, desc, schema, _handler in TOOLS
]


# -------------------------------------------------------------------- protocol

def _result(request_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(message: dict) -> Optional[dict]:
    """One JSON-RPC message in, at most one out (notifications get no reply)."""
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        asked = params.get("protocolVersion")
        return _result(request_id, {
            "protocolVersion": asked if asked in KNOWN_PROTOCOLS else DEFAULT_PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": TOOL_LIST})
    if method == "tools/call":
        name = params.get("name")
        handler = HANDLERS.get(name)
        if not handler:
            return _error(request_id, -32602, f"unknown tool: {name}")
        args = params.get("arguments") or {}
        try:
            payload = handler(args)
            text = payload if isinstance(payload, str) else json.dumps(
                payload, ensure_ascii=False, indent=2)
            return _result(request_id, {"content": [{"type": "text", "text": text}]})
        except ApiError as exc:
            log(f"tool {name} failed: {exc}")
            # An error the agent can read and act on beats a protocol-level
            # failure it can only retry blindly.
            return _result(request_id, {
                "content": [{"type": "text", "text": f"Error: {exc}"}],
                "isError": True,
            })
        except Exception as exc:  # noqa: BLE001 — a crash here kills the reply
            log(f"tool {name} crashed: {type(exc).__name__}: {exc}")
            return _result(request_id, {
                "content": [{"type": "text",
                             "text": f"Error: {type(exc).__name__}: {exc}"}],
                "isError": True,
            })
    if request_id is None:
        return None
    return _error(request_id, -32601, f"unknown method: {method}")


def main() -> None:
    log(f"start pid={os.getpid()} base={API_BASE} key={'set' if API_KEY else 'MISSING'}")
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            log(f"dropped a non-JSON line ({len(line)} chars)")
            continue
        try:
            response = handle(message)
        except Exception as exc:  # noqa: BLE001
            log(f"handler crashed: {type(exc).__name__}: {exc}")
            response = _error(message.get("id"), -32603, str(exc))
        if response is not None:
            out.write(json.dumps(response, ensure_ascii=False) + "\n")
            out.flush()
    log("stdin closed — exiting")


if __name__ == "__main__":
    main()
