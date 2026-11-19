"""kanban-agent CLI — look at the kanban, hand cards to the agent.

  kanban-agent cards [COLUMN ...] [--json]   cards of the given columns (default: kanban.scope)
  kanban-agent kick TASK ... [--say TEXT]    start a turn on each card, as if @Agent were written there
  kanban-agent find QUERY ... [--json]       cards anywhere on the board: by number, or words in the title
  kanban-agent move TASK ... --to COLUMN     move cards to a column of the spec
  kanban-agent say TASK (--html H | --html-file F) [--text T]   post to the card's chat
  kanban-agent show TASK [--json]            the card: title, column, description, whole chat

COLUMN is a role from kanban.columns (bugs) or its title (Bugs). TASK is a card
number (PRO-2062, 2062) or a task UUID. Columns, the scope and the default kick
text come from the flow spec's config.yaml (see settings.py). `say` posts as the
API key's user and runs the message through chatmd like the MCP server does.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from settings import STATE_DIR, conf  # noqa: E402

API_BASE = os.environ.get("YOUGILE_API_BASE", "https://yougile.com/api-v2").rstrip("/")
API_KEY = os.environ.get("YOUGILE_API_KEY", "")
RECEIVER = f"http://{os.environ.get('HOST', '127.0.0.1')}:{os.environ.get('PORT', '9100')}"
# role -> {title, id}
COLUMNS: Dict[str, Dict[str, str]] = conf("kanban.columns", "KANBAN_COLUMNS", {}, dict)
SCOPE: List[str] = conf("kanban.scope", "KANBAN_SCOPE", list(COLUMNS), list)
KICK_TEXT = conf("kick.text", "KICK_TEXT", "Take this card.")
STICKER_NAME = conf("sticker.name", "AGENT_STICKER_NAME", "Agent")


def api(path: str, method: str = "GET", body: Optional[dict] = None) -> Any:
    headers = {"Authorization": f"Bearer {API_KEY}"}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API_BASE + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"null")


def board_columns() -> List[Dict[str, Any]]:
    """Every column of the board the spec's columns live on, not only the spec's own."""
    first = next(iter(COLUMNS.values()))
    board = api(f"/columns/{first['id']}")["boardId"]
    return [c for c in api(f"/columns?boardId={board}&limit=1000").get("content", [])
            if not c.get("deleted")]


def board_tasks() -> List[tuple[Dict[str, Any], str]]:
    """(task, column title) for every live card on the board."""
    out = []
    for col in board_columns():
        for t in api(f"/tasks?columnId={col['id']}&limit=1000").get("content", []):
            if not (t.get("archived") or t.get("deleted")):
                out.append((t, col["title"]))
    return out


def column(name: str) -> Dict[str, str]:
    for role, col in COLUMNS.items():
        if name.lower() in (role.lower(), col["title"].lower()) or name == col["id"]:
            return col
    sys.exit(f"unknown column {name!r}; the spec has: {', '.join(COLUMNS)}")


def sticker() -> tuple[Optional[str], Dict[str, str]]:
    """(sticker id, {state id: state name}) of the agent's status sticker."""
    for s in api("/string-stickers?limit=1000").get("content", []):
        if (s.get("name") or "").strip().lower() == STICKER_NAME.lower() and not s.get("deleted"):
            return s["id"], {st["id"]: st["name"] for st in s.get("states", [])}
    return None, {}


def agent_user_id() -> Optional[str]:
    try:
        return json.loads((STATE_DIR / "agent.json").read_text()).get("agent_user_id")
    except (OSError, ValueError):
        return None


def card_facts(task: Dict[str, Any], column: str, sticker_id: Optional[str],
               states: Dict[str, str], me: Optional[str]) -> Dict[str, Any]:
    msgs = api(f"/chats/{task['id']}/messages?limit=100").get("content", [])
    msgs = sorted((m for m in msgs if not m.get("deleted")), key=lambda m: int(m["id"]))
    last_agent = max((int(m["id"]) for m in msgs if m.get("fromUserId") == me), default=0)
    # Messages from people since the agent last spoke: what a kick would answer.
    unseen = [m for m in msgs if int(m["id"]) > last_agent and m.get("fromUserId") != me]
    state_id = (task.get("stickers") or {}).get(sticker_id or "")
    status = states.get(state_id) if state_id else None
    return {
        "pro": task.get("idTaskProject"),
        "id": task["id"],
        "title": task.get("title", ""),
        "column": column,
        "status": status,
        # Handed = the agent has touched it: a session, a reply or a sticker
        # (cards worked from a terminal have the last two but no session).
        "handed": bool(status or last_agent
                       or (STATE_DIR / "chats" / f"{task['id']}.json").exists()),
        "running": (STATE_DIR / "turns" / f"{task['id']}.json").exists(),
        "unseen": len(unseen),
        "last_agent_at": (datetime.fromtimestamp(last_agent / 1000, timezone.utc).isoformat()
                          if last_agent else None),
    }


def cmd_cards(args: argparse.Namespace) -> None:
    sticker_id, states = sticker()
    me = agent_user_id()
    cards = []
    for name in args.columns or SCOPE:
        col = column(name)
        tasks = api(f"/tasks?columnId={col['id']}&limit=1000").get("content", [])
        for t in tasks:
            if not (t.get("archived") or t.get("deleted")):
                cards.append(card_facts(t, col["title"], sticker_id, states, me))
    if args.json:
        print(json.dumps(cards, ensure_ascii=False, indent=1))
        return
    for c in cards:
        flags = ("handed" if c["handed"] else "new") + (" running" if c["running"] else "")
        print(f"{c['pro']:<9} {c['column']:<12} {c['status'] or '-':<10} {flags:<14} "
              f"unseen={c['unseen']:<2} {c['title'][:70]}")


def resolve_task(ref: str) -> str:
    if re.fullmatch(r"[0-9a-f-]{36}", ref):
        return ref
    num = re.sub(r"\D", "", ref)
    for t, _ in board_tasks():
        if re.sub(r"\D", "", str(t.get("idTaskProject", ""))) == num:
            return t["id"]
    sys.exit(f"card {ref} not found on the board")


def cmd_find(args: argparse.Namespace) -> None:
    found = []
    for t, col in board_tasks():
        pro = str(t.get("idTaskProject", ""))
        title = t.get("title", "")
        for q in args.query:
            num = re.sub(r"\D", "", q) if re.fullmatch(r"(?i)(pro-?)?\d+", q) else ""
            if (num and re.sub(r"\D", "", pro) == num) or (not num and q.lower() in title.lower()):
                found.append({"pro": pro, "id": t["id"], "column": col, "title": title})
                break
    if args.json:
        print(json.dumps(found, ensure_ascii=False, indent=1))
        return
    for c in found:
        print(f"{c['pro']:<9} {c['column']:<16} {c['title'][:90]}")


def cmd_move(args: argparse.Namespace) -> None:
    target = column(args.to)
    titles = {c["id"]: c["title"] for c in board_columns()}
    for ref in args.tasks:
        task_id = resolve_task(ref)
        before = titles.get(api(f"/tasks/{task_id}").get("columnId"), "?")
        api(f"/tasks/{task_id}", "PUT", {"columnId": target["id"]})
        print(f"{ref}: {before} -> {target['title']}")


def cmd_say(args: argparse.Namespace) -> None:
    import chatmd  # the MCP server's repair: raw markup never reaches the reader
    # "-" reads stdin: the Mac wrapper runs this over ssh, where a local file path means nothing.
    if args.html_file == "-":
        html_in = sys.stdin.read()
    else:
        html_in = Path(args.html_file).read_text(encoding="utf-8") if args.html_file else args.html
    text, text_html, notes = chatmd.normalize_message(args.text, html_in)
    if not (text or text_html):
        sys.exit("refusing to post an empty message")
    res = api(f"/chats/{resolve_task(args.task)}/messages", "POST",
              {"text": text, "textHtml": text_html or ""})
    print(f"{args.task}: posted {(res or {}).get('id')}"
          + (f" (repaired: {'; '.join(notes)})" if notes else ""))


def cmd_show(args: argparse.Namespace) -> None:
    import chatmd
    task_id = resolve_task(args.task)
    task = api(f"/tasks/{task_id}")
    titles = {c["id"]: c["title"] for c in board_columns()}
    users = {u["id"]: u.get("realName") or u.get("email") or u["id"][:8]
             for u in api("/users?limit=1000").get("content", [])}
    msgs = api(f"/chats/{task_id}/messages?limit=1000").get("content", [])
    chat = []
    for m in sorted((m for m in msgs if not m.get("deleted")), key=lambda m: int(m["id"])):
        # `text` can be empty when only textHtml was sent; the id is the post time in ms.
        body = (m.get("text") or "").strip() or chatmd.html_to_text(m.get("textHtml") or "")
        chat.append({"at": datetime.fromtimestamp(int(m["id"]) / 1000, timezone.utc)
                     .strftime("%Y-%m-%d %H:%M"),
                     "from": users.get(m.get("fromUserId"), m.get("fromUserId")),
                     "text": body})
    card = {"pro": task.get("idTaskProject"), "id": task_id, "title": task.get("title", ""),
            "column": titles.get(task.get("columnId"), "?"),
            "description": chatmd.html_to_text(task.get("description") or ""), "chat": chat}
    if args.json:
        print(json.dumps(card, ensure_ascii=False, indent=1))
        return
    print(f"{card['pro']} · {card['column']} · {card['title']}\n\n{card['description']}\n")
    for m in chat:
        print(f"--- {m['at']} UTC · {m['from']}\n{m['text']}\n")


def cmd_kick(args: argparse.Namespace) -> None:
    token = (STATE_DIR / "kick.token").read_text().strip()
    for ref in args.tasks:
        body = json.dumps({"task_id": resolve_task(ref), "text": args.say or KICK_TEXT}).encode()
        req = urllib.request.Request(f"{RECEIVER}/kick", data=body, method="POST", headers={
            "Content-Type": "application/json", "X-Kick-Token": token})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                out = json.loads(r.read())
        except urllib.error.HTTPError as e:
            out = {"ok": False, "reason": f"http {e.code}: {e.read().decode()[:200]}"}
        state = ("queued" if out.get("reason") == "queued-capacity"
                 else "started" if out.get("ok") else "NOT started")
        print(f"{ref}: {state} ({out.get('reason')})")


def main() -> None:
    p = argparse.ArgumentParser(prog="kanban-agent", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cards", help="list cards with the agent's status")
    c.add_argument("columns", nargs="*")
    c.add_argument("--json", action="store_true")
    k = sub.add_parser("kick", help="hand cards to the agent")
    k.add_argument("tasks", nargs="+")
    k.add_argument("--say", help="the instruction, instead of kick.text")
    f = sub.add_parser("find", help="find cards anywhere on the board")
    f.add_argument("query", nargs="+", help="PRO-2062 / 2062, or words in the title")
    f.add_argument("--json", action="store_true")
    m = sub.add_parser("move", help="move cards to a column of the spec")
    m.add_argument("tasks", nargs="+")
    m.add_argument("--to", required=True, help="column role (review) or title (On review)")
    s = sub.add_parser("say", help="post a message to a card's chat")
    s.add_argument("task")
    s.add_argument("--text", help="plain-text fallback; built from the HTML when omitted")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--html", help="raw HTML (<h4>, <p>, <ul><li>, <code>, <a href>)")
    g.add_argument("--html-file", help="file with the raw HTML; - reads stdin")
    w = sub.add_parser("show", help="print a card with its description and whole chat")
    w.add_argument("task")
    w.add_argument("--json", action="store_true")
    args = p.parse_args()
    {"cards": cmd_cards, "kick": cmd_kick, "find": cmd_find, "move": cmd_move,
     "say": cmd_say, "show": cmd_show}[args.cmd](args)


if __name__ == "__main__":
    main()
