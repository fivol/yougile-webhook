"""Durable last-draft-wins delivery for one final task message per turn."""
from pathlib import Path
import threading
import usage_summary

_lock = threading.Lock()


def queue(root: Path, chat_id: str, body: dict) -> bool:
    """Buffer a reply only for a running turn that explicitly enables single-reply mode."""
    turn = usage_summary.read_json(root / 'turns' / f'{chat_id}.json')
    target = turn.get('reply_path')
    if not target or not turn.get('single_reply'):
        return False
    path = Path(target)
    # A unique outbox file keeps finalized turns separate from subsequent replies.
    if usage_summary.read_json(path).get('ready'):
        return True
    usage_summary.write_json(path, {'chat_id': chat_id, 'body': body, 'ready': False})
    return True


def deliver(path: Path, send) -> bool:
    """Send a finalized outbox item; retain failures for the background retry loop."""
    with _lock:
        entry = usage_summary.read_json(path)
        if entry.get('sent'):
            return True
        if not entry.get('ready'):
            return False
        if not send(entry['chat_id'], entry['body']):
            return False
        entry['sent'] = True
        usage_summary.write_json(path, entry)
        return True
