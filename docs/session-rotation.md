# Session rotation

A YouGile task chat used to map to exactly one claude session, forever. That is
comfortable — the agent remembers everything — but the bill grows with the
conversation, and past a point most of what you pay for is re-reading context
the agent no longer needs.

Rotation retires a session once carrying it gets expensive and starts a fresh
one on the same task, handing it a **handoff file** instead of the transcript.

## Why it costs what it costs

Every turn re-sends the whole conversation. What varies is the rate:

| | multiplier on input price |
|---|---|
| served from prompt cache | **0.1×** |
| re-written into cache (1h TTL) | **2×** |

A 20× swing, decided by whether the cache entry for this chat's history is
still alive. Measured across 88 real turns on this machine, that is almost
entirely a function of how long the chat sat idle:

| idle before the turn | turns | cache miss |
|---|---:|---:|
| < 10 min | 45 | **7 %** |
| 10–30 min | 21 | 24 % |
| 30–60 min | 4 | 75 % |
| > 60 min | 18 | **100 %** |

The 1-hour TTL holds exactly as documented. The residual misses under 10
minutes are not about time at all: the surviving prefix on a miss is always a
discrete 15–24k tokens — the shared `tools` + system block, kept warm by every
other chat on the machine — and its size *changes between runs*. That is an MCP
server failing to connect at process start, which shortens the tool list; since
tools render first, everything after them is invalidated regardless of age.

Six real sessions, priced at API list rates:

| session | context peak | write | read | output | total |
|---|---:|---:|---:|---:|---:|
| session-1 | 660k | $33 | $137 | $25 | **$196** |
| session-2 | 537k | $31 | $85 | $13 | **$130** |
| session-3 | 504k | $60 | $77 | $9 | **$146** |
| session-4 | **953k** | $159 | $262 | $42 | **$463** |
| session-5 | 395k | $39 | $41 | $7 | **$88** |
| session-6 | **923k** | $212 | $230 | $21 | **$463** |

≈ $1486 total, of which **$536 (36 %) is cache-write** — re-reading what had
already been read — and $832 is cache-read of a context that had grown so large
that even a hit cost $0.45 a turn. Two sessions came within 5 % of the 1M
context window.

## The decision

Everything the decision needs is free and known *before* the turn runs:
`ctx_tokens` from the previous turn's `usage` block, idle time from the clock.
So there is no need to predict whether a particular turn will hit the cache —
only to price it:

```
E[$] = ctx_tokens × ( p(miss | idle) × 2.0 + (1 − p(miss | idle)) × 0.1 )
                   × PRICE_IN_PER_MTOK / 1e6
```

Rotate when that exceeds `ROTATE_TURN_COST_USD`, or when `ctx_tokens` passes
`ROTATE_HARD_CTX` regardless of price (the context window is a wall, not a
budget).

At the default $1.50 that works out to:

| idle | rotates at context ≥ |
|---|---:|
| < 10 min | ~1.3M — effectively never |
| 10–30 min | ~540k |
| 30–60 min | ~200k |
| > 60 min | ~150k |

Which is the behaviour you want: a live back-and-forth keeps its context
because it is cheap while warm, and a chat picked up the next morning resets,
because its context would be re-billed at the write rate.

`p(miss | idle)` is **self-calibrating** — `state/cache_stats.json` records the
outcome of every turn and is seeded with the table above, so the estimate is
right from the first rotation and tracks MCP-server reliability over time.

## What carries over

The handoff (`state/handoff/<chatId>/<seq>.md`) is assembled mechanically, at
zero model cost, from sources that are all free to read:

1. **Task card** — title, description, deadline (`GET /tasks/<id>`).
2. **The complete YouGile chat**, not the 20-message window a normal first turn
   gets.
3. **What the previous session concluded** — its own replies, final todo state,
   files it edited, PRs it opened, pulled out of the old transcript. A 6 MB
   transcript compresses to ~12 KB this way, because the dialogue is a tiny
   fraction of it; the rest is tool traffic.
4. **Workspace state** from the `feature` skill's `.feature.json`: worktree,
   slug, and per repo the branch, base, PR URL and dev-server URL.
5. **Git facts** per repo — `log --oneline base..HEAD` and `diff --stat`.
6. **A pointer to the old transcript**, so anything not captured can be grepped
   rather than guessed at.

A typical handoff is ~25 KB (≈7k tokens). Past `HANDOFF_DIGEST_THRESHOLD_KB`
it is compressed by a cheap model in a **fresh** session — never by resuming
the expensive one, which would pay exactly the cost being avoided.

The new session's prompt tells it to read the handoff, then run
`git diff` per repo and check PR comments, before acting. It is explicitly told
not to open a new branch or PR: `.feature.json` is re-pointed at the new session
id (and gains a `chat_id`, which never changes) so the `feature` skill resumes
the same workspace.

## Modes

| `SESSION_ROTATE_MODE` | behaviour |
|---|---|
| `off` | disabled; identical to v1.3 |
| `manual` | never rotates; posts one advisory per session when the threshold trips, suggesting `/new` |
| `auto` | rotates and posts a notice with the full cost breakdown |

**Ship on `manual`.** Watch the advisories for a few days, confirm they fire
where you would have wanted them to, then switch to `auto`.

The notice the chat gets on rotation:

```
⚙️ Новая сессия

Прошлая — <session-uuid>
  контекст     395 181 токенов · 360 запросов к API
  потрачено    ≈ $88.01   (запись $39.46 · чтение $41.18 · вывод $7.34)
  мимо кеша    13 запросов из 360

Причина: пауза 9 д 13 ч, кеш почти наверняка мёртв → следующий ход на
этом контексте стоил бы ≈ $3.76 (порог $1.50).

Новая — <session-uuid>. Перенесены история задачи,
ветки, PR и дифф.

Старая сессия никуда не делась:
claude --resume <session-uuid>
```

Nothing is deleted — every retired session is still openable locally with
`claude --resume`, and `/sessions` lists the whole chain.

## When it deliberately does nothing

- **A turn was interrupted.** A mid-flight redirect resumes the same session;
  continuity matters more than the saving. An explicit `/new` still wins.
- **No transcript on disk.** Context size is unknown, so nothing is estimated
  and nothing rotates; `/status` says so.
- **Below `ROTATE_MIN_CTX`.** Small sessions are never worth resetting.
- **The chat is hot.** At the default threshold a conversation with replies
  every few minutes will not rotate until it is enormous.

## Configuration

See the `Session rotation`, `Handoff` and `Pricing` blocks in `.env.example`.
The two you will actually touch:

```bash
SESSION_ROTATE_MODE=manual     # -> auto once you trust it
ROTATE_TURN_COST_USD=1.50      # lower = smaller contexts, more resets
```

Prices default to Anthropic API list rates. On a subscription the absolute
figures are notional; the hit/miss ratio that drives the decision is unchanged.
