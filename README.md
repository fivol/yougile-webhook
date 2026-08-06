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
| `TURN_FAILURE_REPORT` | Default `true`. Post a status message into the chat when a turn dies without the agent answering — exit code plus claude's own error (see [When a turn dies](#when-a-turn-dies--the-chat-always-hears-about-it)). `false` restores the old silent behaviour. |
| `TURN_FAILURE_PREFIX` | Marker prepended to those reports. Default `⚠️`. |
| `TURN_FAILURE_QUOTE_CHARS` | How much of claude's output to quote as the reason. Default `600`. |
| `SESSION_RECOVERY_DELAY_SECONDS` | After a session turns out to be lost, wait this long before replaying the turn in a fresh session — enough for anything queued behind the dead turn to claim the chat first (it would supersede the replay). Default `2`. |
| `CLAUDE_PUMP_DRAIN_SECONDS` | How long to keep draining claude's output after it exits, before giving up on a grandchild (a lingering MCP server) that inherited the pipe and never closed it. Default `10`. |
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

### Rotation — why a task spans several sessions

A session that has been running for days costs real money to resume: every
turn re-sends the whole conversation, and whether that is served from cache
(0.1× input price) or re-written (2×) depends almost entirely on how long the
chat has been idle. Because both inputs — context size and idle time — are
known for free *before* a turn starts, the receiver can price the next turn in
advance and retire the session when it gets too expensive, handing the new one
a **handoff file** (task chat, workspace, branches, PR diffs) instead of the
raw transcript.

Off by default (`SESSION_ROTATE_MODE=manual` only advises). See
**[docs/session-rotation.md](docs/session-rotation.md)** for the measured
numbers, the cost model, and how to tune it.

### Asking without spending — chat commands

`/status` answers "how big has this chat got, what has it cost, and will the
next turn start a new session?" — handled by the receiver itself, so it costs
nothing and never wakes the agent. Also `/sessions`, `/new`, `/keep`, `/stop`,
`/help`. See **[docs/chat-commands.md](docs/chat-commands.md)**.

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
| We interrupted the turn ourselves | Nothing — a newer turn is already running. |

The reason quoted is claude's own output: a recognised startup error
(no session, usage limit, expired login, missing binary, 5xx) when there is
one, otherwise the tail of what it printed. It is read from a per-turn
in-memory buffer, never from a byte range of the shared `logs/claude.log` —
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
| `rules.toml`, `prompts/*.txt` | Your real rules and prompts (**gitignored**). |
| `.env.example` / `.env` | Runtime config (`.env` gitignored). |
| `state/chats/<chatId>.json` | Per-chat session state (gitignored). |
| `state/agent.json` | The agent's own YouGile user id, learned from a mention (gitignored). |
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
