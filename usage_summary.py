"""Reprice task transcripts at a dated, explicit standard API price snapshot."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False))
    tmp.replace(path)


def lines(path: Path):
    try:
        with path.open() as source:
            for raw in source:
                try:
                    value = json.loads(raw)
                    if isinstance(value, dict):
                        yield value
                except ValueError:
                    continue
    except OSError:
        return


def claude_records(paths: list[Path]) -> dict:
    records = {}
    for path in paths:
        for i, event in enumerate(lines(path)):
            message = event.get('message') or {}
            usage = message.get('usage')
            if event.get('type') != 'assistant' or not isinstance(usage, dict):
                continue
            key = 'claude:' + str(message.get('id') or f'{path}:{i}')
            # Later blocks can finish the output-token counter for the same response.
            records[key] = {'provider': 'claude', 'model': message.get('model', ''),
                            'usage': usage, 'at': event.get('timestamp')}
    return records


def codex_records(paths: list[Path]) -> dict:
    records = {}
    for path in paths:
        model = ''
        for event in lines(path):
            payload = event.get('payload') or {}
            if event.get('type') == 'turn_context':
                model = payload.get('model') or model
            if event.get('type') != 'token_usage_record' or not isinstance(payload.get('usage'), dict):
                continue
            key = 'codex:' + str(payload.get('response_id') or json.dumps(payload, sort_keys=True))
            records[key] = {'provider': 'codex', 'model': payload.get('model') or model,
                            'usage': payload['usage'], 'at': event.get('timestamp')}
    return records


def price(record: dict, prices: dict) -> tuple[int, float | None]:
    """Return consumed tokens (including cache) and standard API-equivalent USD."""
    u = record['usage']
    model = re.sub(r'-\d{8}$', '', record['model'])
    rates = prices['models'].get(model)
    output = u.get('output_tokens', 0) or 0
    if record['provider'] == 'claude':
        fresh = u.get('input_tokens', 0) or 0
        read = u.get('cache_read_input_tokens', 0) or 0
        write = u.get('cache_creation_input_tokens', 0) or 0
        tokens = fresh + read + write + output
        if not tokens:
            return 0, 0.0
        if not rates:
            return tokens, None
        ttl = u.get('cache_creation') or {}
        hour = ttl.get('ephemeral_1h_input_tokens', 0) or 0
        minute = ttl.get('ephemeral_5m_input_tokens', 0) or 0
        # Missing TTL data must not silently turn into a precise cost.
        if hour + minute != write:
            return tokens, None
        cost = fresh*rates['input'] + read*rates['read'] + hour*rates['write_1h'] + minute*rates['write_5m'] + output*rates['output']
        cost += (u.get('server_tool_use') or {}).get('web_search_requests', 0) * 10_000
    else:
        total_input = u.get('input_tokens', 0) or 0
        read = u.get('cached_input_tokens', 0) or 0
        write = u.get('cache_write_input_tokens', 0) or 0
        tokens = total_input + output  # reasoning tokens are already part of output
        if not tokens:
            return 0, 0.0
        if not rates:
            return tokens, None
        fresh = max(0, total_input - read - write)
        long = total_input > rates.get('long_context_threshold', float('inf'))
        cost = ((fresh*rates['input'] + read*rates['read'] + write*rates['write']) * (2 if long else 1)
                + output*rates['output'] * (1.5 if long else 1))
    return tokens, cost / 1_000_000


def task_records(state: dict, chat_id: str, workdir: str, home: Path, codex_home: Path) -> tuple[dict, bool]:
    """Collect native sessions, retired sessions and locally persisted child agents."""
    ids = {state.get('session_id') or chat_id}
    ids.update(state.get('auxiliary_claude_session_ids', []))
    ids.update(s.get('id') for s in state.get('sessions', []) if s.get('id'))
    slug = str(Path(workdir).resolve()).replace('/', '-').replace('.', '-')
    project = home / '.claude/projects' / slug
    paths = []
    missing = False
    for sid in ids:
        path = project / f'{sid}.jsonl'
        if path.exists():
            paths.append(path)
            paths.extend((project / sid / 'subagents').glob('*.jsonl'))
        elif len(ids) > 1 or state.get('active_provider') != 'codex':
            missing = True
    records = claude_records(paths)
    codex_ids = set(state.get('codex_session_ids', []))
    if state.get('codex_session_id'):
        codex_ids.add(state['codex_session_id'])
    if codex_ids:
        index, parents = {}, {}
        for path in (codex_home / 'sessions').glob('**/*.jsonl'):
            first = next(lines(path), {})
            meta = first.get('payload') or {}
            sid = meta.get('id')
            if not sid:
                continue
            index[sid] = path
            source = meta.get('source')
            if isinstance(source, dict):
                subagent = source.get('subagent')
                spawn = subagent.get('thread_spawn', {}) if isinstance(subagent, dict) else {}
                if spawn.get('parent_thread_id'):
                    parents[sid] = spawn['parent_thread_id']
        while True:
            children = {sid for sid, parent in parents.items() if parent in codex_ids}
            if children <= codex_ids:
                break
            codex_ids |= children
        missing |= bool(codex_ids - index.keys())
        c_records = codex_records([index[sid] for sid in codex_ids if sid in index])
        if not c_records:
            missing = True
        records.update(c_records)
    return records, missing


def summary(records: dict, prices: dict, started_at: str, missing: bool = False) -> dict:
    total_tokens = turn_tokens = 0
    cost = 0.0
    unknown = set()
    for record in records.values():
        tokens, usd = price(record, prices)
        total_tokens += tokens
        if record.get('at') and datetime.fromisoformat(record['at'].replace('Z', '+00:00')) >= datetime.fromisoformat(started_at.replace('Z', '+00:00')):
            turn_tokens += tokens
        if usd is None:
            unknown.add(record['model'] or 'unknown')
        else:
            cost += usd
    return {'task_tokens': total_tokens, 'turn_tokens': turn_tokens,
            'api_usd': cost, 'incomplete': missing or not records or bool(unknown),
            'unknown_models': sorted(unknown), 'price_date': prices['checked_at']}


def footer(label: str, totals: dict, started_at: str, ended_at: str) -> str:
    """Render one compact footer without exposing session internals."""
    start = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
    end = datetime.fromisoformat(ended_at.replace('Z', '+00:00'))
    seconds = max(0, int((end-start).total_seconds()))
    duration = f'{seconds//60} мин {seconds%60:02d} с'
    fmt = lambda n: f'{n:,}'.replace(',', ' ')
    price_text = (f"≥ ${totals['api_usd']:.2f} (неполные данные)" if totals['incomplete']
                  else f"≈ ${totals['api_usd']:.2f}")
    return (f"{label} · ход {duration}\n"
            f"Токены: ход {fmt(totals['turn_tokens'])} / задача {fmt(totals['task_tokens'])} · "
            f"API за задачу {price_text}")
