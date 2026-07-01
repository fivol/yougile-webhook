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
rule set so you can see what's wired up. It is **non-blocking** — it never
calls the YouGile API, so it stays a reliable liveness signal even when the
network is down. It reports the last-known webhook subscription snapshot
(refreshed by the background watcher) under `webhook`, and `loop_heartbeat_age_s`
(seconds since the event loop last ticked — see the watchdog below).

### Network resilience

The receiver is built to ride out an unstable network without losing work or
going silently dead:

- **Force IPv4** (`NETWORK_FORCE_IPV4=true`, default) — a broken/half-open IPv6
  route is the usual cause of `SSL handshake timed out` on an otherwise-healthy
  host: `urllib` has no Happy-Eyeballs, so it stalls on the dead AAAA address
  until timeout while `curl` connects instantly over IPv4. Dropping AAAA results
  avoids it. Falls back to IPv6 if a host has no A record.
- **Retries everywhere** — every YouGile API call and every attachment download
  retries transient failures (network error, timeout, HTTP 429/5xx) with capped
  exponential backoff; a 4xx is permanent and never retried.
- **Self-healing subscription** — the watcher revives a YouGile-disabled
  subscription, and after a failed cycle retries in `WEBHOOK_ENSURE_RETRY_SECONDS`
  (default 60s) instead of waiting the full sweep interval.
- **Event-loop watchdog** — if the asyncio loop ever wedges, the process
  force-exits so launchd (`KeepAlive`) restarts a fresh one. Network outages do
  NOT trigger it (a restart wouldn't help those).

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
# @Agent — unified entry point. Agent classifies the latest mention as
# RESEARCH or IMPLEMENT and acts accordingly. Posts its own ack
# ("Исследую" / "Начал работу" / "Продолжаю …") with the session id.
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
skip_if_chat_known = true                                    # don't double-fire if @Agent already ran
# workdir = "/path/to/your/company-root"                     # override per-rule
# extra_args = "--dangerously-skip-permissions --model sonnet"
```

### `@Agent` flow

`@Agent` is the only chat entry point. The moment a mention matches a
rule, the receiver drops a 👍 reaction on that message (configurable via
`ACK_REACTION_EMOJI`) as an instant "received" signal — before the agent
even spawns. The agent then reads the latest mention together with the
task title/description, chat history, and any attached screenshots, then
picks ONE of two modes:

- **RESEARCH** — questions / analysis / investigation. Agent answers
  in chat with no code changes. Ack: `Исследую` + session id.
- **IMPLEMENT** — code changes (add / fix / refactor / extend / tests).
  Ack: `Начал работу` + session id (first time) or `Продолжаю в ветке
  task-<slug>` + session id (continuation).

In IMPLEMENT mode the agent keeps **one worktree + one branch + one PR
per task chat** (base = `dev`):

1. **First time:** for each affected repo:
   - `git worktree add worktrees/<slug>/<repo> -b task-<slug> origin/dev`
     (worktrees live under the company root, `dev` is required)
   - Implements + commits in the worktree
   - `git push -u origin task-<slug>`
   - `gh pr create --base dev` → captures the PR URL
   - Posts ONE chat message listing each touched repo's branch name and
     PR URL.
2. **Continuation** (same chat, follow-up `@Agent`): agent re-uses the
   existing worktree/branch, adds commits, `git push` — the existing PR
   auto-updates. No second PR.
3. **Fresh branch/PR**: only when the user explicitly asks for one
   ("новая ветка", "новый PR", "fresh PR"). Then the first-time flow
   runs again with a new slug.

Claude's session memory (`--session-id <chatId>` first time, `--resume
<chatId>` after) is what lets the agent remember the slug / worktree
path / PR URL across turns.

| Field | Meaning |
|---|---|
| `name` | Identifier shown in logs and `/healthz`. |
| `enabled` | `true` / `false`. |
| `events` | YouGile event types (`chat_message-created`, `task-created`, `task-moved`, `task-updated`, …). |
| `pattern` | Optional regex over the full event JSON. |
| `allowed_sender_emails` | Whitelist; resolved to user UUIDs at startup via `/api-v2/users`. Empty = no sender filter (not recommended). |
| `column_names` | Match `payload.columnId` against the resolved UUIDs of these column titles. |
| `column_transition_only` | Default `true`. When `column_names` is set, only fire on actual transitions (`prevData.columnId != payload.columnId`). |
| `skip_if_chat_known` | Default `false`. Skip this rule if a claude session already exists for the task's chat (`state/chats/<chatId>.json` exists). Use it on auto-kickoff rules like `task_in_progress` so they don't pile a second LLM run on top of an already-active `@Agent` conversation. |
| `session_per_chat` | Default `true`. Runs claude with `--session-id <chatId>` first time, `--resume <chatId>` after. |
| `prompt_file` | Path to a text file used as the prompt. `{event_json}`, `{chat_history}`, `{first_turn}`, `{formatting}`, `{language}`, and `{session_id}` are substituted. |
| `workdir` | Override `CLAUDE_WORKDIR` for this rule. |
| `extra_args` | Override `CLAUDE_EXTRA_ARGS` for this rule. |
| `ack_message` | Optional short plain-text reply posted into the task chat the moment claude is successfully spawned (e.g. `"Взял в работу..."`). The session id (== chat/task UUID) is auto-appended on its own line so you can copy it from the task and continue the same session locally with `claude --resume <id>`. Fires only after the subprocess actually started, so a failed launch never leaves an orphan ack. Opt-in — omitted = no ack. |
| `ack_message_html` | Optional HTML companion to `ack_message`. Usually unneeded — the session id line is appended automatically with `<code>` wrapping for clean copy. |
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
| `CLAUDE_MAX_CONCURRENT` | Hard cap on concurrent claude processes across DISTINCT chats (default 10). A re-mention in an already-running chat interrupts that turn instead of counting here. When a genuinely new chat hits the cap, it gets `CONCURRENCY_BUSY_MESSAGE` rather than a silent drop. |
| `CONCURRENCY_BUSY_MESSAGE` / `_HTML` | Message posted into a chat when the global cap is saturated by OTHER chats (so the user knows to retry shortly). Empty = stay silent. |
| `INTERRUPT_SIGKILL_GRACE_TICKS` | When a new `@Agent` message interrupts a running turn for the same chat, how many 0.1s ticks to wait after `SIGTERM` before `SIGKILL` (lets claude flush its session transcript). Default `30` (~3s). |
| `CLAUDE_API_RETRIES` | Total attempts (incl. the first) when the spawned claude exits non-zero AND its output matches a transient Anthropic API error (`API Error: 5xx`, `Overloaded`, `overloaded_error`, `rate_limit_error`). Default `3`. |
| `CLAUDE_RETRY_DELAYS` | Comma-separated backoff in seconds between retries. Default `30,120,300`. The last value repeats if there are more retries than entries. |
| `RULES_FILE` | Default `rules.toml`. |
| `WEBHOOK_ENSURE_INTERVAL_SECONDS` | How often the service sweeps its own YouGile subscription and revives it if YouGile auto-disabled it after a streak of failed deliveries (e.g. while we were down). Default `900` (15 min). The same check runs once at startup. Set to a large value to disable the timer; the startup check still fires. |
| `WEBHOOK_ENSURE_RETRY_SECONDS` | After a *failed* ensure cycle (couldn't reach YouGile / couldn't revive the subscription), retry this soon instead of waiting the full interval — so recovery from an outage takes minutes. Default `60`. |
| `YOUGILE_API_TIMEOUT` | Per-attempt timeout (seconds) for every call to the YouGile API. Default `30`. |
| `YOUGILE_API_MAX_RETRIES` | Retries for data-plane calls (reaction, ack, history fetch, message post) on a *transient* failure — network error, timeout, HTTP 429/5xx — with capped exponential backoff. `0` = retry forever (default), so a flapping VPN or brief outage never drops a 👍 or an ack. A 4xx is permanent and never retried. |
| `YOUGILE_API_BOOT_RETRIES` | Bounded retry budget for boot/control-plane calls (directory load, webhook (de)registration) so a dead API at startup can't hang the import; the watcher just retries next cycle. Default `2`. |
| `YOUGILE_API_WATCHER_RETRIES` | Retry budget for the background subscription watcher's `ensure()` — it runs off the request path, so it affords more than the boot budget before backing off. Default `5`. |
| `YOUGILE_API_RETRY_BASE_DELAY` / `YOUGILE_API_RETRY_MAX_DELAY` | Backoff between API retries — starts at base, doubles, capped at max (seconds). Defaults `1` / `30`. |
| `NETWORK_FORCE_IPV4` | Force IPv4 for all outbound connections by dropping AAAA (IPv6) DNS results — sidesteps the `SSL handshake timed out` stalls a broken IPv6 route causes with `urllib` (no Happy-Eyeballs). Falls back to IPv6 if a host has no A record. Default `true`. |
| `LOOP_WATCHDOG_TIMEOUT_SECONDS` / `LOOP_WATCHDOG_CHECK_SECONDS` | Event-loop watchdog: if the asyncio loop stops heart-beating for longer than the timeout, the process force-exits so launchd restarts it. Only catches a genuinely wedged loop (threshold ≫ any normal pause), never a network outage. `0` timeout disables. Defaults `180` / `15`. |
| `SENDER_KEYS` | Payload keys checked (in order, top-level and nested) for the sender's user UUID. |
| `COLUMN_KEYS` | Same, for column UUIDs. |
| `CHAT_ID_KEYS` | Same, for the task/chat UUID used as the claude session id. |
| `CHAT_HISTORY_FIRST_TIME_LIMIT` | How many recent messages to inject on the first trigger for a chat (default 20). |
| `CHAT_HISTORY_DELTA_LIMIT` | How many new messages to inject on subsequent triggers (default 50). |
| `ATTACHMENTS_ENABLED` | Default `true`. Download `/root/#file:<url>` attachments from chat messages into `state/attachments/<chatId>/` and surface them to the agent. |
| `ATTACHMENT_MAX_BYTES` | Per-file size cap (default 25 MiB). Larger files are skipped. |
| `ATTACHMENT_TIMEOUT_SECONDS` | Per-file download timeout (default 60s). |
| `ATTACHMENT_MAX_RETRIES` | Retry a transient attachment download (network error, timeout, 5xx) this many times before giving up; a 4xx or over-size file is permanent and not retried. Default `3`. |
| `ACK_REACTION_EMOJI` | Emoji reaction dropped on the trigger message the instant it matches a rule — fires before the agent spawns and before any `ack_message`, as an immediate "received" signal. Default `👍`. YouGile accepts ONLY `👍 👎 👏 🙂 😀 😕 🎉 ❤ 🚀 ✔` (👀 is rejected). Empty = disabled. Applies only to `chat_message-*` events. |

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
- While claude is running for a chat, a new `@Agent` message for the SAME
  chat **interrupts** the in-flight turn (see below) — it is never dropped.

To reset a single chat: `rm state/chats/<chatId>.json`. The next trigger
will start a fresh session and re-inject full history.

### Interrupting a running turn

A new `@Agent` message that lands while the agent is still working on the
same chat does **not** queue or get dropped — it takes over immediately:

1. The in-flight turn's process group is `SIGTERM`-ed (then `SIGKILL` after
   `INTERRUPT_SIGKILL_GRACE_TICKS` × 0.1s, so claude can flush its session).
2. The instant it dies, a fresh turn starts that `--resume`s the same
   session, with the new message in its delta history and an explicit
   "you were interrupted" notice prepended to the prompt.
3. The agent re-reads the latest message as the authoritative instruction,
   checks its workspace/git state (the previous turn may have stopped
   mid-edit), and continues, extends, or changes direction accordingly.

This is how you redirect the agent mid-task — just `@Agent` again with the
new instruction. Webhook returns `spawn_reason: interrupting`. Because an
interrupt replaces an existing turn 1-for-1, it never counts against
`CLAUDE_MAX_CONCURRENT`; that cap only limits genuinely distinct chats.

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
`ATTACHMENT_TIMEOUT_SECONDS` (60s default). A transient failure is retried up
to `ATTACHMENT_MAX_RETRIES` times (default 3) with backoff; files that exceed
the cap or still fail are silently skipped and their markers stay unresolved.

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
