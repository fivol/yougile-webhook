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
cp prompts/implement.example.txt       prompts/implement.txt
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

A list of trigger rules, evaluated top to bottom; **first match wins** —
put the most specific rules first.

```toml
# Implement command: "!implement ..." — agent clarifies or implements
# the task end-to-end across affected repos, opens one PR into `dev`
# per repo, and posts the PR links back into the chat.
[[rules]]
name = "chat_implement"
enabled = true
events = ["chat_message-created"]
pattern = "!implement\\b"
allowed_sender_emails = ["you@example.com"]
prompt_file = "prompts/implement.txt"

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

### `!implement` flow

When someone writes `!implement ...` in a task chat:

1. Agent fetches the full task (title + description) and reads the chat
   history (including any image attachments).
2. **If the task is ambiguous** — agent posts the smallest set of
   clarifying questions back into the chat and stops. The user answers
   and re-triggers with `!implement` again. The claude session is
   preserved across both invocations (same `chatId`), so context
   carries over.
3. **If the task is clear** — for each affected repository:
   - `git worktree add worktrees/<slug>/<repo> -b task-<slug> origin/dev`
     (worktrees live under the company root, `dev` is required)
   - Implements + commits in the worktree
   - `git push -u origin task-<slug>`
   - `gh pr create --base dev` → captures the PR URL
4. Posts ONE chat message with all PR links (one per touched repo).

The implement rule must come **above** `chat_mention` in `rules.toml` so
that `@Agent !implement ...` routes to implement, not to chat-mention.

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
| `prompt_file` | Path to a text file used as the prompt. `{event_json}`, `{chat_history}`, `{first_turn}`, `{formatting}`, and `{language}` are substituted. |
| `workdir` | Override `CLAUDE_WORKDIR` for this rule. |
| `extra_args` | Override `CLAUDE_EXTRA_ARGS` for this rule. |
| `ack_message` | Optional short plain-text reply posted into the task chat the moment claude is successfully spawned (e.g. `"Взял в работу..."`). Fires only after the subprocess actually started, so a failed launch never leaves an orphan ack. Opt-in — omitted = no ack. |
| `ack_message_html` | Optional HTML companion to `ack_message`. Usually unneeded — keep acks short and plain. |
| `language` | Output language hint, substituted into prompts as `{language}` (e.g. `"ru"`). Lets you declare the reply language once in the rules file instead of repeating it in every prompt. |

## Configure runtime — `.env`

| Variable | Meaning |
|---|---|
| `YOUGILE_API_KEY` | Required. Used by `register_webhook.py` and for resolving emails/columns at startup. |
| `YOUGILE_API_BASE` | Default `https://yougile.com/api-v2`. |
| `YOUGILE_FILE_BASE_URL` | Origin used to resolve schemeless `/user-data/...` attachment URLs that YouGile inlines in chat messages. Default: derived from `YOUGILE_API_BASE` (e.g. `https://yougile.com`). |
| `YOUGILE_WEBHOOK_URL` | Public URL of this receiver. |
| `YOUGILE_WEBHOOK_EVENT` | Event regex for the YouGile subscription. Keep broad (`(chat_message\|task)-.*` or `.*`). |
| `HOST` / `PORT` | Bind address (default `127.0.0.1:9100`). |
| `CLAUDE_BIN` | Path to the `claude` CLI. |
| `CLAUDE_WORKDIR` | Default workdir for spawned claude processes. |
| `CLAUDE_EXTRA_ARGS` | Default CLI args. |
| `CLAUDE_MAX_CONCURRENT` | Hard cap on concurrent claude processes. Excess events are dropped. |
| `CLAUDE_API_RETRIES` | Total attempts (incl. the first) when the spawned claude exits non-zero AND its output matches a transient Anthropic API error (`API Error: 5xx`, `Overloaded`, `overloaded_error`, `rate_limit_error`). Default `3`. |
| `CLAUDE_RETRY_DELAYS` | Comma-separated backoff in seconds between retries. Default `30,120,300`. The last value repeats if there are more retries than entries. |
| `RULES_FILE` | Default `rules.toml`. |
| `SENDER_KEYS` | Payload keys checked (in order, top-level and nested) for the sender's user UUID. |
| `COLUMN_KEYS` | Same, for column UUIDs. |
| `CHAT_ID_KEYS` | Same, for the task/chat UUID used as the claude session id. |
| `CHAT_HISTORY_FIRST_TIME_LIMIT` | How many recent messages to inject on the first trigger for a chat (default 20). |
| `CHAT_HISTORY_DELTA_LIMIT` | How many new messages to inject on subsequent triggers (default 50). |
| `ATTACHMENTS_ENABLED` | Default `true`. Download `/root/#file:<url>` attachments from chat messages into `state/attachments/<chatId>/` and surface them to the agent. |
| `ATTACHMENT_MAX_BYTES` | Per-file size cap (default 25 MiB). Larger files are skipped. |
| `ATTACHMENT_TIMEOUT_SECONDS` | Per-file download timeout (default 30s). |

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

## Attachments (images & files)

YouGile embeds files inline in chat messages as
`/root/#file:<url>`. With `ATTACHMENTS_ENABLED=true` (default) every such
URL found in the trigger payload and in fetched history is downloaded
once into `state/attachments/<chatId>/` and the marker is rewritten in
the rendered history as `[image: /abs/path]` or `[file: /abs/path]`. The
agent can then open the local file with its `Read` tool — images render
visually for vision-capable models.

A list of every downloaded path is also appended to the prompt under
`Attachments referenced above are saved locally`, so the agent can see
at a glance what's available without parsing the history.

Caps: `ATTACHMENT_MAX_BYTES` (25 MiB default) and
`ATTACHMENT_TIMEOUT_SECONDS` (30s default). Files that exceed the cap
or time out are silently skipped and their markers stay unresolved.

To send files back to chat, the prompts point the agent at
`mcp__yougile-mcp__send_task_file` (taskId = `payload.chatId`,
filePath = local path).

## Replying from the agent — shared formatting

Every spawned agent should produce visually consistent messages — same
density, same heading discipline, same link style. The single source of
truth is **`prompts/_formatting.txt`**: it's loaded once at startup and
substituted into every prompt that contains `{formatting}` (and appended
as a fallback to prompts that don't). Override with `FORMATTING_FILE` env
var.

Edit `prompts/_formatting.txt` once and every rule picks up the change
on next restart — no per-prompt copy-paste. The shipped file enforces:

- `textHtml` is what YouGile actually renders; `text` is the plain
  fallback. Set both for any non-trivial reply.
- No `<h1>`/`<h2>` — they render enormous in chat. Use `<h4>` for the
  top heading of a multi-section message, `<b>` for sub-section labels.
- One blank line maximum between sections; no stacked `<br><br>` or
  empty `<p></p>` for spacing.
- One `<p>` per paragraph, no `<p>` inside `<li>`, no blank lines
  between `<li>` items.
- Always wrap URLs in `<a href="...">label</a>` with a human-readable
  label.
- Never put markdown (`**bold**`, backticks, `#`, `|` tables, `-`/`*`
  bullets) into `text` — YouGile shows it as raw characters.

If a particular HTML tag renders as literal text in your deployment,
drop it from `prompts/_formatting.txt`.

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
| `state/attachments/<chatId>/` | Locally-cached chat attachments (gitignored). |
| `logs/events.jsonl` | All inbound webhooks. |
| `logs/claude.log` | Spawned claude stdout/stderr. |

## Security

- `--dangerously-skip-permissions` removes all claude confirmations.
  `allowed_sender_emails` is your only barrier against arbitrary task
  participants triggering shell commands on your machine. Keep it minimal.
- `.env`, `rules.toml`, `prompts/*.txt` (non-example) are gitignored. Don't
  commit them.
- If you publish via a tunnel, its token is a separate secret.
