# yougile-webhook

Receive YouGile webhooks and spawn `claude --dangerously-skip-permissions`
on configurable triggers. Each YouGile task chat maps 1:1 to a persistent
claude session, so the agent remembers prior turns when re-mentioned.

```
YouGile ──POST──▶ https://your.domain/yougile/webhook
                      │
                      ▼ (FRP / cloudflared / ngrok / nginx on a VPS — your choice)
                      ▼
                  127.0.0.1:9100  (this service)
                      │
                      ▼ first matching rule in rules.toml
                      ▼
                  claude --dangerously-skip-permissions
                  cwd = rule.workdir
                  --session-id <chatId>   (first time on this chat)
                  --resume     <chatId>   (subsequent)
```

## Install

```bash
git clone <this-repo>
cd yougile-webhook
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
cp rules.example.toml rules.toml
cp prompts/chat_mention.example.txt    prompts/chat_mention.txt
cp prompts/task_in_progress.example.txt prompts/task_in_progress.txt
# edit .env, rules.toml, and the prompts
```

Make `https://your.domain/yougile/webhook` publicly reach `127.0.0.1:9100`
via whatever you prefer (FRP, cloudflared, ngrok, nginx on a VPS).

## Run

```bash
./run.sh
curl http://127.0.0.1:9100/healthz
```

`run.sh` reads `.env` and starts uvicorn. `/healthz` returns the resolved
rule set so you can see what's wired up.

### Autostart (macOS launchd)

`~/Library/LaunchAgents/org.local.yougile-webhook.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>org.local.yougile-webhook</string>
    <key>ProgramArguments</key>
    <array><string>/PATH/TO/yougile-webhook/run.sh</string></array>
    <key>WorkingDirectory</key><string>/PATH/TO/yougile-webhook</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/PATH/TO/yougile-webhook/logs/uvicorn.log</string>
    <key>StandardErrorPath</key><string>/PATH/TO/yougile-webhook/logs/uvicorn.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/Users/USERNAME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
```

`PATH` must include the directory of the `claude` CLI, or the spawned
process won't find it.

```bash
launchctl load -w ~/Library/LaunchAgents/org.local.yougile-webhook.plist
launchctl list | grep yougile-webhook
```

## Register the YouGile webhook

One subscription with a broad event regex covers all rules:

```bash
.venv/bin/python register_webhook.py list                 # current subscriptions
.venv/bin/python register_webhook.py create               # create from .env
.venv/bin/python register_webhook.py delete <hook-id>     # delete one
```

Defaults from `.env.example`:

```
YOUGILE_WEBHOOK_URL=https://your.public.domain/yougile/webhook
YOUGILE_WEBHOOK_EVENT=(chat_message|task)-.*
```

## Configure rules — `rules.toml`

A list of trigger rules, evaluated top to bottom; **first match wins**.

```toml
[[rules]]
name = "chat_mention"
enabled = true
events = ["chat_message-created"]
pattern = "@Agent"
allowed_sender_emails = ["you@example.com"]
prompt_file = "prompts/chat_mention.txt"

[[rules]]
name = "task_in_progress"
enabled = false                                              # toggle when ready
events = ["task-created", "task-moved", "task-updated"]
column_names = ["In progress"]
allowed_sender_emails = ["you@example.com"]
prompt_file = "prompts/task_in_progress.txt"
# workdir = "/path/to/your/company-root"                     # override per-rule
# extra_args = "--dangerously-skip-permissions --model sonnet"
```

| Field | Meaning |
|---|---|
| `name` | Identifier shown in logs and `/healthz`. |
| `enabled` | `true` / `false`. |
| `events` | YouGile event types (`chat_message-created`, `task-created`, `task-moved`, `task-updated`, …). |
| `pattern` | Optional regex over the full event JSON. |
| `allowed_sender_emails` | Whitelist; resolved to user UUIDs at startup via `/api-v2/users`. Empty = no sender filter (not recommended). |
| `column_names` | Match `payload.columnId` against the resolved UUIDs of these column titles. |
| `column_transition_only` | Default `true`. When `column_names` is set, only fire on actual transitions (`prevData.columnId != payload.columnId`). |
| `session_per_chat` | Default `true`. Runs claude with `--session-id <chatId>` first time, `--resume <chatId>` after. |
| `prompt_file` | Path to a text file used as the prompt. `{event_json}` and `{chat_history}` are substituted. |
| `workdir` | Override `CLAUDE_WORKDIR` for this rule. |
| `extra_args` | Override `CLAUDE_EXTRA_ARGS` for this rule. |

## Configure runtime — `.env`

| Variable | Meaning |
|---|---|
| `YOUGILE_API_KEY` | Required. Used by `register_webhook.py` and for resolving emails/columns at startup. |
| `YOUGILE_API_BASE` | Default `https://yougile.com/api-v2`. |
| `YOUGILE_WEBHOOK_URL` | Public URL of this receiver. |
| `YOUGILE_WEBHOOK_EVENT` | Event regex for the YouGile subscription. Keep broad (`(chat_message\|task)-.*` or `.*`). |
| `HOST` / `PORT` | Bind address (default `127.0.0.1:9100`). |
| `CLAUDE_BIN` | Path to the `claude` CLI. |
| `CLAUDE_WORKDIR` | Default workdir for spawned claude processes. |
| `CLAUDE_EXTRA_ARGS` | Default CLI args. |
| `CLAUDE_MAX_CONCURRENT` | Hard cap on concurrent claude processes. Excess events are dropped. |
| `RULES_FILE` | Default `rules.toml`. |
| `SENDER_KEYS` | Payload keys checked (in order, top-level and nested) for the sender's user UUID. |
| `COLUMN_KEYS` | Same, for column UUIDs. |
| `CHAT_ID_KEYS` | Same, for the task/chat UUID used as the claude session id. |
| `CHAT_HISTORY_FIRST_TIME_LIMIT` | How many recent messages to inject on the first trigger for a chat (default 20). |
| `CHAT_HISTORY_DELTA_LIMIT` | How many new messages to inject on subsequent triggers (default 50). |

After editing `.env` or `rules.toml`, reload:

```bash
launchctl unload  ~/Library/LaunchAgents/org.local.yougile-webhook.plist
launchctl load -w ~/Library/LaunchAgents/org.local.yougile-webhook.plist
```

## Sessions and chat history

For rules with `session_per_chat = true` (default):

- `session_id = chatId` (the YouGile task UUID).
- State lives at `state/chats/<chatId>.json` — one tiny file per chat,
  containing `last_message_id` and timestamps.
- Existence of that file decides `--resume` vs `--session-id`.
- The prompt receives chat history via the `{chat_history}` placeholder
  (full on first run, only new messages on subsequent runs).
- While claude is running for a chat, new triggers for the SAME chat are
  dropped with `spawn_reason: chat-busy`. No queue.

To reset a single chat: `rm state/chats/<chatId>.json`. The next trigger
will start a fresh session and re-inject full history.

## Replying from the agent

YouGile chat does **not** render markdown. The agent prompts already
instruct claude to reply in plain text and, when richer formatting is
needed, pass `textHtml` (simple HTML — `<p>`, `<b>`, `<a>`, `<code>`,
`<br>`) alongside the `text` plain fallback.

## Reference: real YouGile payloads

`chat_message-created`:

```json
{
  "event": "chat_message-created",
  "fromUserId": "<user-uuid>",
  "payload": {
    "id": 1779102737006,
    "chatId": "<task-uuid>",
    "text": "@Agent ...",
    "reactions": {},
    "properties": {"params": {"chunks": []}}
  },
  "prevData": { ... }
}
```

`task-created`:

```json
{
  "event": "task-created",
  "fromUserId": "<user-uuid>",
  "payload": {
    "id": "<task-uuid>",
    "title": "...",
    "description": "...",
    "columnId": "<column-uuid>",
    "boardId": "<board-uuid>",
    "projectId": "<project-uuid>",
    "createdBy": "<user-uuid>",
    "archived": false,
    "completed": false
  }
}
```

`task-moved` — same as `task-created` plus `prevData.columnId` with the
previous column.

## Files

| Path | Content |
|---|---|
| `app.py` | FastAPI receiver and rule engine. |
| `run.sh` | Loads `.env` and exec's uvicorn. |
| `register_webhook.py` | CLI to manage YouGile subscriptions. |
| `rules.example.toml` | Rule template (commit). |
| `prompts/*.example.txt` | Prompt templates (commit). |
| `rules.toml`, `prompts/*.txt` | Your real rules and prompts (**gitignored**). |
| `.env.example` / `.env` | Runtime config (`.env` gitignored). |
| `state/chats/<chatId>.json` | Per-chat session state (gitignored). |
| `logs/events.jsonl` | All inbound webhooks. |
| `logs/claude.log` | Spawned claude stdout/stderr. |

## Security

- `--dangerously-skip-permissions` removes all claude confirmations.
  `allowed_sender_emails` is your only barrier against arbitrary task
  participants triggering shell commands on your machine. Keep it minimal.
- `.env`, `rules.toml`, `prompts/*.txt` (non-example) are gitignored. Don't
  commit them.
- If you publish via a tunnel, its token is a separate secret.
