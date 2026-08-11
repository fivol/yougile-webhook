# yougile-webhook

Receive YouGile webhooks and spawn `claude --dangerously-skip-permissions`
on configurable triggers. Each YouGile task chat maps 1:1 to a persistent
claude session, so the agent remembers prior turns when re-mentioned —
whether you type `@Agent` or just
[reply to its message](#addressing-the-agent--mention-or-reply). A
[sticker](#the-status-sticker--is-the-agent-still-working) on the task
card tracks the agent: `working` while it runs, `answered` when there's a
reply to read.

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

## Requirements

| What | Why |
|---|---|
| Python 3.11+ | this service |
| The `claude` CLI, logged in | every turn is a `claude -p` process |
| Node 18+ and a **YouGile MCP server** | how the agent talks back to the chat — see below |
| A YouGile API key | this service reads chat history and drives the status sticker |

### The YouGile MCP server — not optional

This receiver is one half of the loop. It *starts* an agent and hands it the
chat history; everything the agent sends **back** — the ack, the answer,
files, task updates — goes through MCP tools, which come from a separate
YouGile MCP server. Without it the agent runs, works, and says nothing:
`/healthz` is green, the sticker flips to `working`, and the chat stays
silent.

The prompts shipped here call `mcp__yougile-mcp__send_task_message`,
`…__send_task_file`, `…__get_task_messages`, `…__get_task` and
`…__update_task`. Any MCP server exposing those names works; the one these
prompts were written against is
[github.com/ichinya/yougile-mcp](https://github.com/ichinya/yougile-mcp)
(note: the unrelated `yougile-mcp` package on npm is a different project —
don't `npx` it):

```bash
git clone https://github.com/ichinya/yougile-mcp ~/mcp-servers/yougile-mcp
cd ~/mcp-servers/yougile-mcp && npm install && npm run build
```

Register it for the account that runs the agent. The turn is spawned as
`claude … -p` with `cwd = rule.workdir` and no `--mcp-config`, so the server
has to be reachable from there — **user scope** (available in every
directory) is the simple choice:

```bash
claude mcp add --scope user yougile-mcp \
  --env YOUGILE_API_KEY=<your-key> \
  -- node ~/mcp-servers/yougile-mcp/yougile.cjs
```

Verify from the workdir, not from this repo — that's where the agent runs:

```bash
cd /path/to/your/company-root && claude mcp list
```

A project-scoped `.mcp.json` inside `rule.workdir` works too. The MCP server
needs its own copy of the API key: it authenticates as the account the agent
posts **as**, which is usually a dedicated bot user, while this service's
`YOUGILE_API_KEY` only reads history and stickers.

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

`version` echoes `APP_VERSION` from `app.py` (also logged on startup and
served as the OpenAPI version). It is hand-maintained: bump it with any
change worth restarting for, then check `/healthz` after the restart to
confirm the new code is the code that's actually running.

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

**Exactly one live subscription per URL.** Two enabled rows for the same URL
make YouGile deliver every event twice. The service enforces this on every
`ensure` cycle (startup + timer): it keeps one canonical subscription, prunes
every other row (dead or duplicate-live), and re-enables the survivor —
recreating only when there is genuinely none. `register_webhook.py create` also
refuses to add a second live subscription, so you can't create a duplicate by
hand either. You normally never need `create` at all — the service self-provisions.

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

`@Agent` is the only chat entry point — either typed, or **implied by
replying to one of the agent's own messages** (see
[Addressing the agent](#addressing-the-agent--mention-or-reply)). The
moment a mention matches a rule, the receiver drops a 👍 reaction on that
message (configurable via
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

### Two prompts per session

Because the session is resumed, everything the first prompt said is still in
the agent's context. Re-sending it costs a full re-read of text it has not
forgotten, and — being appended at the end — it competes for attention with the
actual request. So a rule has two templates:

| | `prompt_file` | `prompt_file_followup` |
|---|---|---|
| Used on | first turn of a session (incl. after a rotation) | every turn after that |
| Carries | the standing instructions: how to classify, which skills to use, the full formatting rules, the whole chat so far | the new messages, the stats block, and a short reminder of what is cheap to get wrong |

What a follow-up turn no longer carries:

- **The raw webhook event.** It was ~340 tokens of JSON, half of it `prevData`
  — a byte-for-byte copy of `payload` — wrapped around a message that is
  already the last line of `{chat_history}`. The trigger message is now simply
  the newest entry there, with its time, sender, text and attachments.
  `{event_json}` still works in older templates, rendering those facts as three
  lines instead of a JSON blob.
- **The agent's own posts.** It wrote them minutes ago and still has them; in
  practice they were 90%+ of a follow-up's history block.
- **Slash commands** and the receiver's replies to them, as before.

Both are dropped from the *prompt* only — the watermark still steps over them,
or they would be re-fetched forever.

If `prompt_file_followup` is missing, nothing changes: every turn gets the full
template, exactly as before.

| Field | Meaning |
|---|---|
| `name` | Identifier shown in logs and `/healthz`. |
| `enabled` | `true` / `false`. |
| `events` | YouGile event types (`chat_message-created`, `task-created`, `task-moved`, `task-updated`, …). |
| `pattern` | Optional regex over the full event JSON. |
| `allowed_sender_emails` | Whitelist; resolved to user UUIDs at startup via `/api-v2/users`. Empty = no sender filter (not recommended). |
| `column_names` | Match `payload.columnId` against the resolved UUIDs of these column titles. |
| `column_transition_only` | Default `true`. When `column_names` is set, only fire on actual transitions (`prevData.columnId != payload.columnId`). |
| `skip_if_chat_known` | Default `false`. Skip this rule if the task's chat already has a claude session — either finished (`state/chats/<chatId>.json` exists) or **being launched / running right now** (the chat's turn lock is held). Use it on auto-kickoff rules like `task_in_progress` so they don't pile a second LLM run on top of an already-active `@Agent` conversation. Such a rule is never queued behind a live turn: if the chat is busy the event is dropped, not postponed, so moving a task to "In progress" while the agent is mid-reply can't fire a stale kickoff once that reply finishes. |
| `session_per_chat` | Default `true`. Runs claude with `--session-id <chatId>` first time, `--resume <chatId>` after. |
| `prompt_file` | Path to a text file used as the prompt for the FIRST turn of a session. `{chat_history}`, `{chat_id}`, `{first_turn}`, `{formatting}`, `{language}`, `{session_id}`, `{session_stats}` and `{handoff}` are substituted. |
| `prompt_file_followup` | Prompt used from the SECOND turn of a session onward — see [Two prompts per session](#two-prompts-per-session). Defaults to `<prompt_file>` with `.followup.txt` in place of `.txt`, so dropping the file in beside the main one is enough. Omit the file entirely to keep re-sending the full template. |
| `workdir` | Override `CLAUDE_WORKDIR` for this rule. |
| `extra_args` | Override `CLAUDE_EXTRA_ARGS` for this rule. |
| `ack_message` | Optional short plain-text reply posted into the task chat the moment claude is successfully spawned (e.g. `"Взял в работу..."`). The session id (== chat/task UUID) is auto-appended on its own line so you can copy it from the task and continue the same session locally with `claude --resume <id>`. Fires only after the subprocess actually started, so a failed launch never leaves an orphan ack. Opt-in — omitted = no ack. |
| `ack_message_html` | Optional HTML companion to `ack_message`. Usually unneeded — the session id line is appended automatically with `<code>` wrapping for clean copy. |
| `language` | Output language hint, substituted into prompts as `{language}` (e.g. `"ru"`). Lets you declare the reply language once in the rules file instead of repeating it in every prompt. |

## Configure runtime — `config.yaml` and `.env`

Behaviour settings live in **`config.yaml`**, grouped (`claude`, `history`,
`turn`, `rotation`, `handoff`, `chat`) instead of spread across seventy flat
variables. Override them per machine in **`config.local.yaml`** (gitignored),
which is deep-merged on top.

Environment variables still win over both, so every existing `.env` keeps
working unchanged and a one-off override needs no file edit. Precedence,
strongest first:

    environment  >  config.local.yaml  >  config.yaml  >  built-in default

Secrets stay in `.env` — `config.yaml` is meant to be committed. Each key in
it names the variable that overrides it, so nothing has to be looked up twice.

> **Booleans.** Write `true` / `false`. Bare `yes` / `no` / `on` / `off` are
> deliberately read as **strings**: YAML 1.1 would turn them into booleans, and
> a two-letter language code like `no` (Norwegian) becoming `False` is exactly
> the kind of bug that takes an afternoon to find.

Everything below can be set as an environment variable; the ones that also have
a `config.yaml` home are marked in that file.

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
| `TURN_FAILURE_REPORT` | Default `true`. Post a status message into the chat when a turn dies without the agent answering — exit code plus claude's own error (see [When a turn dies](#when-a-turn-dies--the-chat-always-hears-about-it)). `false` restores the old silent behaviour. |
| `TURN_FAILURE_PREFIX` | Marker prepended to those reports. Default `⚠️`. |
| `TURN_FAILURE_QUOTE_CHARS` | How much of claude's output to quote as the reason. Default `600`. |
| `SESSION_RECOVERY_DELAY_SECONDS` | After a session turns out to be lost, wait this long before replaying the turn in a fresh session — enough for anything queued behind the dead turn to claim the chat first (it would supersede the replay). Default `2`. |
| `TURN_REPORT_SILENT` | Default `true`. Post a note when a turn ends cleanly but the agent never wrote into the chat. `false` restores the old silence. |
| `TURN_LOG_KEEP` | Per-turn output logs kept in `logs/turns/`. Default `200`. |
| `TURN_ADOPT_POLL_SECONDS` | How often to poll a turn adopted from a previous receiver process (it is no longer a child, so it cannot be `wait()`ed on). Default `5`. |
| `RULES_FILE` | Default `rules.toml`. |
| `MESSAGES_LANG` | Which `messages/<lang>.toml` the receiver speaks. Default `ru`. See [Wording](#wording--messageslangtoml). |
| `MESSAGES_FILE` | Absolute path to a messages file, overriding `MESSAGES_LANG`. |
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
| `AGENT_STATUS_STICKER` | Default `true`. Show the agent's state on the task card via a sticker: `working` while a turn runs, `answered` when it stops (see [The status sticker](#the-status-sticker--is-the-agent-still-working)). `false` leaves task cards alone. |
| `AGENT_STICKER_NAME` / `AGENT_STICKER_ICON` | The sticker is looked up by name and created on first use if missing. Defaults `Agent` / `pin`. |
| `AGENT_STICKER_WORKING` / `AGENT_STICKER_ANSWERED` | State names, matched exactly (case-insensitively) against the sticker's states. Defaults `working` / `answered`. YouGile's API can rename a sticker but **cannot edit its states**, so changing these after the sticker exists means adding the state by hand in the UI. |
| `AGENT_STICKER_WORKING_COLOR` / `AGENT_STICKER_ANSWERED_COLOR` | Hex colours, used only when the sticker is created. Defaults `#8CC2FF` / `#79CE69`. |
| `REPLY_TO_AGENT_IS_MENTION` | Default `true`. Treat a reply to (or forward of) one of the agent's own messages as a mention, so `@Agent` rules fire on it without the text containing the mention. |
| `AGENT_MENTION` | The mention token your rules match on and that YouGile renders for the agent's account. Default `@Agent`. |
| `AGENT_USER_EMAIL` / `AGENT_USER_ID` | Which account the agent posts as — needed to tell a reply to the *agent* from a reply to a colleague. Normally learned automatically from the first `@Agent …` message and cached in `state/agent.json`; set either to pin it (and to arm reply-detection before any mention has been seen). `/healthz` reports the resolved id. |
| `REPLY_QUOTE_MAX_CHARS` | How much of the replied-to message to quote into the prompt so the agent knows which of its own messages the user is answering. Default `2000`. |
| `ACK_REACTION_EMOJI` | Emoji reaction dropped on the trigger message the instant it matches a rule — fires before the agent spawns and before any `ack_message`, as an immediate "received" signal. Default `👍`. YouGile accepts ONLY `👍 👎 👏 🙂 😀 😕 🎉 ❤ 🚀 ✔` (👀 is rejected). Empty = disabled. Applies only to `chat_message-*` events. |

After editing `.env` or `rules.toml`, reload:

```bash
launchctl unload  ~/Library/LaunchAgents/org.local.yougile-webhook.plist
launchctl load -w ~/Library/LaunchAgents/org.local.yougile-webhook.plist
```

## Sessions and chat history

For rules with `session_per_chat = true` (default):

- A chat owns a **chain** of claude sessions. Link 0 is the chat id itself
  (the YouGile task UUID); later links are `uuid5`-derived from it, so they
  are deterministic and need no lookup table.
- State lives at `state/chats/<chatId>.json` — one small file per chat with
  `last_message_id`, the current session pointer, what the last turn cost,
  and the retired links of the chain.
- `session_bootstrapped` decides `--resume` vs `--session-id`; the existence
  of the file still means only "we have run for this chat before", which is
  what `skip_if_chat_known` keys off.
- The prompt receives chat history via the `{chat_history}` placeholder
  (full on first run, only new messages on subsequent runs).
- While claude is running for a chat, a new `@Agent` message for the SAME
  chat **interrupts** the in-flight turn (see below) — it is never dropped.

To reset a single chat: `rm state/chats/<chatId>.json`. The next trigger
will start a fresh session and re-inject full history.

### Exactly once: the delivery watermark

`last_message_id` is the id of the newest message that has been **handed to
the agent**. Each turn fetches everything after it, adds the trigger message
if the webhook outran the messages API, and moves the mark to the newest id in
that batch — counting the ones filtered out of the prompt, or they would be
re-fetched on every later turn forever.

The mark moves **the moment the process is live**, not when the turn ends.
`claude -p` writes its prompt into the session transcript at startup, so once
the process exists those messages are in the session and a later `--resume`
will see them even if this turn is killed a second later. Moving the mark at
exit instead is how a turn that never reaches its exit — a receiver restart, a
kill, a crash — makes the next turn re-serve messages the agent has already
answered. The one case that really is "not delivered" is a session claude
refused to open; that rolls the mark back to `prev_watermark` from the turn
record.

If the batch is bigger than `history.delta_limit`, the newest N are sent and
the prompt says how many were left out — silently trimming would drop messages
the agent never saw, because the mark has already stepped past them.

### A turn outlives the receiver

claude runs in its own process session, so restarting the receiver does not
kill a running turn. Everything needed to *finish* that turn therefore lives on
disk too, in `state/turns/<chatId>.json`: the pid, the session, where the
output went, the watermark to roll back, and any instruction queued behind an
interrupt.

- claude's output goes to `logs/turns/<chat>-<ts>.log`, **not** a pipe. A pipe
  has a reader that dies with the receiver, after which claude blocks on a full
  buffer or is killed writing to a closed one — mid-turn, having answered
  nothing. A file has no reader to lose, so an orphaned turn runs to completion
  and still posts its own reply over MCP.
- On startup the receiver reconciles `state/turns/`: a turn whose pid is still
  alive is **adopted** (re-supervised by polling, and its chat marked busy
  again, so nothing `--resume`s a session another process is holding); a turn
  whose pid is gone is **finished right there** — metrics, card, queued
  instruction, and a word to the chat if the agent never got one out.

The prompt of every turn is still appended to `logs/claude.log`, which is the
file to grep when asking "what exactly did we send it?".

### Rotation — why a task spans several sessions

A session that has been running for days costs real money to resume. Every
turn re-sends the whole conversation, and a turn is not one API call but
dozens — measured turns on this machine run 22 to 106 calls, each re-reading
the entire prefix at 0.1× the input price (or 2× on the first one, if the cache
died meanwhile). So the bill scales with context size × turn length, and both
are known for free *before* a turn starts: the receiver prices the next turn in
advance and retires the session when it gets too expensive, handing the new one
a **handoff file** (task chat, workspace, branches, PR diffs) instead of the
raw transcript. In practice that puts the reset point around 100–130k tokens.

Off by default (`SESSION_ROTATE_MODE=manual` only advises). See
**[docs/session-rotation.md](docs/session-rotation.md)** for the measured
numbers, the cost model, and how to tune it.

A rotation is **not** a message of its own. It rides on the ack the agent
posts when it takes the task on — one turn, one message — and reports the
three numbers behind the decision: context, spend so far, and the threshold
that tripped. Same for the `manual`-mode advice. Costs quoted there are the
estimate for the turn *ahead*, since that is what is knowable when the task
is accepted; what a turn actually cost shows up in `/status` afterwards.

### Asking without spending — chat commands

`/status` answers "how big has this chat got, what has it cost, and will the
next turn start a new session?" — handled by the receiver itself, so it costs
nothing and never wakes the agent. Also `/sessions`, `/new`, `/keep`, `/stop`,
`/help`. See **[docs/chat-commands.md](docs/chat-commands.md)**.

Who may run them: `COMMAND_ALLOWED_EMAILS`, or — when that is empty — whoever
the rules already let talk to the agent. The sender is resolved with the same
`SENDER_KEYS` lookup the rules use, because YouGile puts `fromUserId` at the
top level of a chat-message payload rather than inside `payload`. A command
from anyone else is refused **out loud**: a command that silently vanishes is
indistinguishable from a receiver that is down.

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

### When a turn dies — the chat always hears about it

The 👍 on your message means "received", not "answered". If the turn dies
before the agent posts anything, the receiver — not the agent — reports it,
so a dead turn can never look like a slow one:

| What happened | What lands in the chat |
|---|---|
| claude exited non-zero | `⚠️ Ход завершился с ошибкой (код N)` + the last thing claude printed. |
| The turn never launched (`spawn-failed` / `exception`) | `⚠️ Не смог запустить агента` + where to look. |
| The session couldn't be opened | `⚠️ Не смог продолжить сессию этого чата` + the error, then an automatic replay in a fresh session. |
| Global concurrency cap | `CONCURRENCY_BUSY_MESSAGE` (unchanged). |
| The turn exited 0 but the agent posted nothing | `⚠️ Ход завершился, но агент так ничего и не написал` (`turn.report_silent`). |
| A turn was cut off by a receiver restart | `⚠️ Ход оборвался на перезапуске обвязки` — found by the startup reconcile. |
| We interrupted the turn ourselves | Nothing — a newer turn is already running. |

The last two exist because the receiver **never writes the agent's answers** —
the agent posts them itself over MCP — so "the process exited" and "the user
got a reply" are separate facts. When they disagree the chat would otherwise
show a 👍 and then silence forever, which looks exactly like a turn still
running. Before saying so, the receiver asks YouGile whether anything from the
agent's account landed after this turn's messages; if the API can't be reached
it stays quiet rather than accusing a turn that did reply.

The reason quoted is claude's own output: a recognised startup error
(no session, usage limit, expired login, missing binary, 5xx) when there is
one, otherwise the tail of what it printed. It is read from that turn's own
`logs/turns/*.log`, never from a byte range of the shared `logs/claude.log` —
several turns append there at once, so a range can hold another chat's
answer.

**Lost session, and why the chat isn't bricked by one.** `claude --resume
<id>` on a session that is no longer on disk exits 1 within two seconds
having printed one line. That id is then unusable in both directions, so
every later mention would die exactly the same way. When that error is
seen, the receiver retires the session (a normal chain rotation with reason
`session-lost`, visible in `/sessions`), re-points the feature workspace at
the new id so the replay keeps its branch and PR, and re-runs the same turn
**once** in the fresh session. The replay is skipped if a newer turn has
already taken the chat, and never recurses: a second failure is reported and
left alone.

## Addressing the agent — mention or reply

Two things count as "I'm talking to you, agent":

1. Typing the mention (`@Agent …`) — matched by the rule's `pattern`.
2. **Replying to / forwarding one of the agent's own messages.** YouGile
   records that as `properties.replyTo = <message id>` and leaves the text
   mention-free, so on its own it would match no rule. The receiver looks
   the quoted message up and, when the agent wrote it, injects
   `AGENT_MENTION` into the payload:

   ```json
   "_addressedToAgent": {
     "mention": "@Agent",
     "reason": "the user replied to a message the agent posted",
     "replyToMessageId": "<message-id>",
     "replyToText": "…the agent message being answered…"
   }
   ```

   The existing `@Agent` rules then fire unchanged, and the spawned agent
   sees in `{event_json}` both why it was triggered and which of its own
   messages the user is answering. The webhook response reports
   `reply_to_agent: true`.

Replies to a *colleague's* message are ignored, which is why the receiver
has to know the agent's own account. It learns that by itself: YouGile
renders a mention as a `{"type": "user", "replacement": "@Agent", "data":
{"userId": …}}` chunk, so the first real `@Agent …` message names the
account, and it's cached in `state/agent.json`. Set `AGENT_USER_EMAIL` to
skip the wait. Everything here is guarded by `REPLY_TO_AGENT_IS_MENTION`,
costs one API call, and only for messages that actually carry a `replyTo`.

## The status sticker — "is the agent still working?"

With `AGENT_STATUS_STICKER=true` (default) the task card carries a
sticker saying what the agent is doing:

| Sticker | Meaning |
|---|---|
| `working` | A turn is running. The answer isn't in yet — wait. |
| `answered` | The agent stopped and left its reply in the chat — go read it. |
| (none) | The agent has never run on this task. |

A turn sets `working` the instant its process spawns and `answered` when
that process exits. `answered` deliberately does *not* mean "this task is
finished" — a turn that merely answered a question sets it too. The
signal is "your turn, human".

**Why not the `completed` checkbox.** It looks like the natural place for
this, but YouGile rejects a `completed` write — in *both* directions,
`400 Не удалось изменить задачу` — from any account that isn't among the
task's assignees. The checkbox is the assignee's own, so a bot can only
touch it on tasks nobody is assigned to. Stickers have no such rule.

**Setup.** The sticker is looked up by `AGENT_STICKER_NAME` and created
on first use if missing, so there's nothing to do beyond enabling it on
the boards where you want it visible (one-time, in the YouGile UI). Note
that YouGile's API can rename a sticker but **cannot edit its states**:
if you rename `AGENT_STICKER_WORKING` / `_ANSWERED` after the sticker
exists, add the matching state by hand — the receiver logs exactly which
one it couldn't find.

Details worth knowing:

- An interrupt or a follow-up queued mid-turn keeps it on `working`: the
  agent isn't done, so the card never blinks "read me" and back.
- A transient-API retry (`CLAUDE_API_RETRIES`) keeps it on `working` too
  — the turn is still alive.
- A turn that never starts (spawn failure, `CLAUDE_MAX_CONCURRENT`
  saturated, an auto-kickoff that bowed out via `skip_if_chat_known`)
  doesn't touch the card at all.
- The value is written only when it actually differs from what YouGile
  holds, so it neither spams `task-updated` events nor fights a human who
  changed it by hand mid-turn.
- Writes for one task are serialized and always apply the latest intent,
  so "started" and "finished" can't land out of order over a flaky link.
- If the service dies mid-turn the card stays on `working` until the next
  turn for that task ends. Nothing reconciles it at startup — the state
  can't be told apart from a turn that is genuinely still running.
- `/healthz` reports the resolved sticker (`status_sticker`), so you can
  see at a glance whether the marker is armed.

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

### The reply contract — repeated on every turn

The file has two parts, split by a `=== ALWAYS ===` marker line:

- **above it** — the full spec. Substituted at `{formatting}`, i.e. once per
  session, on the first turn;
- **below it** — a ~12-line reply contract, appended **last to every
  prompt**, follow-ups included.

The split exists because presence and attention aren't the same thing. A
turn that runs a `workspace`/`ship`-style skill spends 100k tokens
implementing, and the last output template it read is the skill's, not this
file's. The result is a report with the right blocks and no markup: correct
content, `textHtml` empty, an unformatted wall in the chat. Restating the
contract at the end of every prompt is what keeps that from happening; the
long spec above the marker stays a first-turn cost.

A file with no marker is all spec and behaves exactly as before.

### When a skill owns the report

Skills bring their own chat-output template — which blocks, in which order,
where the links go. That is **what** to say; this file is **how** it reaches
the user. The prompts state the bridge explicitly: keep the skill's blocks,
render them as HTML, send them as one `send_task_message` call. Without that
line the agent resolves the clash by dropping the formatting and keeping the
blocks, which is the failure above.

### Unformatted replies are detected, not just discouraged

The agent never sees how its own message rendered, and YouGile can't
re-render one after the fact (its message `PUT` only takes
`deleted`/`label`/`react`). So the receiver watches its own agent's messages
as they come back as deliveries: a reply of at least
`chat.plain_reply_min_chars` (400) characters that carries no `textHtml` is
logged as a warning and recorded on the chat (`last_reply_plain`). The next
prompt for that chat opens the reply contract with a one-line notice that
the previous answer went out unformatted. One good reply clears the flag.

## Wording — `messages/<lang>.toml`

Everything the **receiver itself** says in a chat — `/help`, `/status`,
`/sessions`, rotation notices, command acks, failure reports — lives in
`messages/ru.toml`, not in `app.py`. The code is English, the chats it
serves are not, and a file of strings is something you can hand to
someone who doesn't read Python.

To translate the service: copy the file, translate the values, point
`MESSAGES_LANG` at it.

```bash
cp messages/ru.toml messages/en.toml   # then translate the values
echo 'MESSAGES_LANG=en' >> .env        # or MESSAGES_FILE=/abs/path.toml
```

Keys are `table.key` paths into the TOML; the code fills `{placeholder}`
slots with already-formatted values (`$12.34`, `1 234`, `2 ч 05 мин`), so a
translator never has to deal with format specs. Reorder or drop slots
freely; an unknown slot name leaves the template unfilled rather than
raising. A missing key logs an ERROR and renders as the bare key — the
turn still completes. A missing or malformed file is logged and the
service still starts.

Four marks are understood and become HTML for the chat plus a plain twin
for notifications — `# heading`, `- item`, `**emphasis**`, `` `literal` ``
and `[label](url)`. Write no HTML in the file: the chat panel is narrow and
wraps, so messages are laid out as short lines joined by `·`, never as
aligned columns. Nouns that follow a number (`61 ход` / `623 хода`) pick
their form from the `[plural]` table.

Deliberately **not** in there:

- `prompts/*.txt` — text sent to the agent, which has its own files.
- `CONCURRENCY_BUSY_MESSAGE` — stays in `.env` where it has always been.
- Interrupt notices and prompt scaffolding — addressed to the model, not
  the user, and English on purpose.
- The quoted `reason` in a failure report — that is claude's own error
  output, passed through verbatim.

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

A mention is rendered as a chunk inside
`payload.properties.params.chunks` — this is what identifies the agent's
account (see [Addressing the agent](#addressing-the-agent--mention-or-reply)):

```json
{"type": "user", "replacement": "@Agent", "data": {"userId": "<user-uuid>"}}
```

A reply carries `payload.properties.replyTo = <id of the quoted message>`
and nothing else — the quoted text is not in the payload, which is why
resolving it costs one API call.

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
| `prompts/_formatting.txt` | Channel rules: how a reply is rendered for YouGile, plus the always-repeated reply contract (commit). |
| `messages/<lang>.toml` | Everything the receiver says in a chat (commit). |
| `rules.toml`, `prompts/*.txt` | Your real rules and prompts (**gitignored**). |
| `config.yaml` | Grouped behaviour settings (commit). |
| `config.local.yaml` | Per-machine overrides, deep-merged on top (**gitignored**). |
| `.env.example` / `.env` | Secrets and env overrides (`.env` gitignored). |
| `state/chats/<chatId>.json` | Per-chat session state (gitignored). |
| `state/turns/<chatId>.json` | The chat's in-flight turn: pid, session, watermark, queued instruction. Exists only while a turn is running (gitignored). |
| `state/agent.json` | The agent's own YouGile user id, learned from a mention (gitignored). |
| `state/attachments/<chatId>/` | Locally-cached chat attachments (gitignored). |
| `logs/events.jsonl` | All inbound webhooks. |
| `logs/claude.log` | Every turn's prompt, plus a pointer to its output log. |
| `logs/turns/*.log` | One file per turn: claude's stdout/stderr. |

## Security

- `--dangerously-skip-permissions` removes all claude confirmations.
  `allowed_sender_emails` is your only barrier against arbitrary task
  participants triggering shell commands on your machine. Keep it minimal.
- `.env`, `rules.toml`, `prompts/*.txt` (non-example) are gitignored. Don't
  commit them.
- If you publish via a tunnel, its token is a separate secret.
