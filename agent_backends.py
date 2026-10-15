"""CLI compatibility, quota detection and portable session context."""
from __future__ import annotations

import json
import re
from pathlib import Path

QUOTA = re.compile(
    r"you(?:'|’)?ve hit your limit|usage limit (?:reached|exceeded)"
    r"|you have reached your (?:usage|weekly|monthly|daily) limit"
    r"|credit balance is too low|extra usage (?:limit|exhausted)", re.I)


def events(output: str):
    """Yield JSON objects from a CLI log, ignoring supervisor markers."""
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            yield value


def quota_failure(output: str) -> bool:
    """Only structured CLI errors can trigger a provider switch."""
    return any(e.get('type') == 'result' and e.get('is_error') is True
               and QUOTA.search(str(e.get('result', '')) + str(e.get('errors', '')))
               for e in events(output))


def codex_result(output: str) -> tuple[str | None, str | None]:
    """Read the native session ID and terminal error, including interrupted runs."""
    session = error = None
    for e in events(output):
        if e.get('type') == 'thread.started':
            session = e.get('thread_id')
        elif e.get('type') == 'turn.failed':
            error = str(e.get('error', 'Codex turn failed'))
    return session, error


def model_name(model: str) -> str:
    """Display a concrete model ID without inventing a version for an alias."""
    match = re.fullmatch(r'claude-(opus|sonnet|haiku)-(\d+)-(\d+)(?:-\d{8})?', model)
    if match:
        return f'{match[1].title()} {match[2]}.{match[3]}'
    if model.startswith('gpt-'):
        return 'GPT-' + model[4:].replace('-', ' ').title()
    return model


def reply_model(root: Path, chat_id: str) -> str:
    """Resolve Claude's actual response model or the explicitly selected Codex model."""
    try:
        state = json.loads((root / 'chats' / f'{chat_id}.json').read_text())
        if state.get('active_provider') == 'codex':
            return model_name(state.get('codex_model', ''))
        workdir = state.get('agent_workdir')
        session = state.get('session_id') or chat_id
        if not workdir:
            return ''
        slug = str(Path(workdir).resolve()).replace('/', '-').replace('.', '-')
        path = Path.home() / '.claude/projects' / slug / f'{session}.jsonl'
        # The tool call requesting this message was just persisted here.
        with path.open('rb') as source:
            source.seek(max(0, path.stat().st_size - 512_000))
            tail = source.read().decode('utf-8', errors='replace')
        models = [e.get('message', {}).get('model') for e in events(tail)
                  if e.get('type') == 'assistant']
        return model_name(next((m for m in reversed(models) if m and m != '<synthetic>'), ''))
    except (OSError, ValueError, TypeError):
        return ''


def option_value(args: list[str], flag: str) -> str | None:
    """Read the final CLI option, supporting both --flag value and --flag=value."""
    value = None
    for i, arg in enumerate(args):
        if arg == flag and i + 1 < len(args):
            value = args[i + 1]
        elif arg.startswith(flag + '='):
            value = arg.split('=', 1)[1]
    return value


def reply_label(root: Path, chat_id: str) -> str:
    """Label replies using the model and the effort captured for this running turn."""
    model = reply_model(root, chat_id)
    if not model:
        return ''
    try:
        state = json.loads((root / 'chats' / f'{chat_id}.json').read_text())
        effort = state.get('agent_effort') or 'CLI default'
    except (OSError, ValueError, TypeError):
        effort = 'unknown'
    return f'{model} · effort: {effort}'


def codex_context(session: str | None, home: Path) -> str:
    """Extract recent dialogue/actions and keep a pointer to the complete native transcript."""
    if not session or not re.fullmatch(r'[0-9a-fA-F-]{36}', session):
        return ''
    paths = list((home / 'sessions').glob(f'**/*{session}.jsonl'))
    if not paths:
        return f'Codex session: {session}. Native transcript unavailable; use the task chat and git.'
    path = paths[0]
    parts = []
    with path.open() as source:
        for line in source:
            for event in events(line):
                payload = event.get('payload') or {}
                if event.get('type') == 'response_item':
                    if payload.get('type') == 'message' and payload.get('role') == 'assistant':
                        parts.extend(b.get('text', '') for b in payload.get('content', [])
                                     if isinstance(b, dict))
                    elif payload.get('type') in ('function_call', 'custom_tool_call'):
                        parts.append(f"Action {payload.get('name')}: " +
                                     str(payload.get('arguments') or payload.get('input') or '')[:3000])
                elif event.get('type') == 'event_msg' and payload.get('type') == 'context_compacted':
                    parts.append(str(payload))
    return (f'Codex session: {session}\nComplete transcript: {path}\n'
            'Recent actions and replies (older details remain in the transcript):\n'
            + '\n\n'.join(parts)[-40_000:])


def codex_command(binary: str, model: str, effort: str, session: str | None,
                  python: str, mcp_script: str, auto_review: bool = False) -> list[str]:
    """Build a noninteractive, sandboxed Codex invocation with the same reply channel."""
    cmd = [binary, *(['--approve-for-me'] if auto_review else []), 'exec']
    if session:
        cmd += ['resume', session]
    if not auto_review:
        cmd += ['-c', 'approval_policy="never"']
    cmd += ['--json', '--model', model,
            '-c', 'sandbox_mode="workspace-write"',
            '-c', 'sandbox_workspace_write.network_access=true',
            '-c', f'model_reasoning_effort={json.dumps(effort)}',
            '-c', f'mcp_servers.yougile-mcp.command={json.dumps(python)}',
            '-c', f'mcp_servers.yougile-mcp.args={json.dumps([mcp_script])}', '-']
    return cmd
