"""board-agent CLI — look at the board, hand cards to the agent.

  board-agent cards [COLUMN ...] [--json]   cards of the spec's columns (default: board.scope)
  board-agent kick TASK ... [--say TEXT]    start a turn on each card, as if @Agent were written there

TASK is a card number (PRO-2062, 2062) or a task UUID. Columns, the scope and
the default kick text come from the flow spec's config.yaml (see settings.py).
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
COLUMNS: Dict[str, str] = conf("board.columns", "BOARD_COLUMNS", {}, dict)
SCOPE: List[str] = conf("board.scope", "BOARD_SCOPE", list(COLUMNS), list)
KICK_TEXT = conf("kick.text", "KICK_TEXT", "Take this card.")
STICKER_NAME = conf("sticker.name", "AGENT_STICKER_NAME", "Agent")


def api(path: str) -> Any:
    req = urllib.request.Request(API_BASE + path, headers={"Authorization": f"Bearer {API_KEY}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"null")


def column_id(name: str) -> str:
    for title, cid in COLUMNS.items():
        if title.lower() == name.lower() or cid == name:
            return cid
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
        cid = column_id(name)
        tasks = api(f"/tasks?columnId={cid}&limit=1000").get("content", [])
        for t in tasks:
            if not (t.get("archived") or t.get("deleted")):
                cards.append(card_facts(t, name, sticker_id, states, me))
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
    for cid in COLUMNS.values():
        for t in api(f"/tasks?columnId={cid}&limit=1000").get("content", []):
            if re.sub(r"\D", "", str(t.get("idTaskProject", ""))) == num:
                return t["id"]
    sys.exit(f"card {ref} not found in the spec's columns")


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
        print(f"{ref}: {'started' if out.get('ok') else 'NOT started'} ({out.get('reason')})")


def main() -> None:
    p = argparse.ArgumentParser(prog="board-agent", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cards", help="list cards with the agent's status")
    c.add_argument("columns", nargs="*")
    c.add_argument("--json", action="store_true")
    k = sub.add_parser("kick", help="hand cards to the agent")
    k.add_argument("tasks", nargs="+")
    k.add_argument("--say", help="the instruction, instead of kick.text")
    args = p.parse_args()
    {"cards": cmd_cards, "kick": cmd_kick}[args.cmd](args)


if __name__ == "__main__":
    main()
