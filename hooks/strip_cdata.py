#!/usr/bin/env python3
"""PreToolUse hook: drop an XML CDATA wrapper off an outgoing chat message.

Every reply this system delivers leaves the agent through
`mcp__yougile-mcp__send_task_message`, and about one in two hundred formatted
replies leaves with `textHtml` wrapped in `<![CDATA[ ... ]]>`. YouGile stores the
string verbatim and its renderer reads the section as a single non-element node,
so the message lands as an EMPTY bubble: the turn exits 0, nothing is logged
anywhere, and the user sees a blank message from the agent. Four times since May.

The agent cannot learn its way out of this — it never sees its own messages
rendered — so the wrapper is removed here instead, before the call is made.
Claude Code feeds the pending tool call in on stdin and runs the tool with
whatever `updatedInput` we hand back.

Wired from ~/.claude/settings.json (user scope, so it covers every workdir the
receiver spawns an agent in). Belt-and-braces companion: app.py flags a reply
that still reaches the chat CDATA-wrapped and says so in the next prompt.

Silent, and exit 0, on anything unexpected — a broken hook must never cost the
user their reply.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

TOOL = "mcp__yougile-mcp__send_task_message"
OPEN, CLOSE = "<![CDATA[", "]]>"
# `textHtml` is where it lands; `text` is checked because a wrapper there would
# show up as literal characters, which is the same bug with a louder symptom.
FIELDS = ("textHtml", "text")
LOG = Path(__file__).resolve().parent.parent / "logs" / "hooks.log"


def unwrap(value):
    """The value without its CDATA wrapper, or None if there was nothing to do."""
    if not isinstance(value, str) or OPEN not in value:
        return None
    stripped = value.strip()
    if stripped.startswith(OPEN) and stripped.endswith(CLOSE):
        return stripped[len(OPEN):-len(CLOSE)].strip()
    # Wrapped in some other shape. Neither token can legitimately appear in a
    # YouGile chat message, so drop them rather than guess at the structure.
    return value.replace(OPEN, "").replace(CLOSE, "").strip()


def note(message):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as fh:
            fh.write(f"{datetime.now(timezone.utc).isoformat()} strip_cdata: {message}\n")
    except OSError:
        pass


def main():
    event = json.load(sys.stdin)
    if event.get("tool_name") != TOOL:
        return
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return

    # Logged for every matched call, not just repaired ones: a hook that never
    # fires and a hook that never finds anything look identical from the chat.
    html = tool_input.get("textHtml")
    note(f"saw task {tool_input.get('taskId')} textHtml={len(html) if isinstance(html, str) else 0}")

    fixed = dict(tool_input)
    repaired = []
    for field in FIELDS:
        clean = unwrap(tool_input.get(field))
        if clean is not None and clean != tool_input.get(field):
            fixed[field] = clean
            repaired.append(field)
    if not repaired:
        return

    fields = ", ".join(repaired)
    note(f"unwrapped {fields}")
    # No `permissionDecision`: the input is all we mean to change, and the
    # normal permission pipeline should still see the call.
    json.dump(
        {
            "systemMessage": (
                f"Removed a CDATA wrapper from {fields} — YouGile renders "
                f"<![CDATA[...]]> as an empty message."
            ),
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "updatedInput": fixed,
            },
        },
        sys.stdout,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
