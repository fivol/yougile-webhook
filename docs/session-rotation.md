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

### One turn is not one API call

The miss is the smaller half of the bill. An agent turn reads, greps, edits,
screenshots and runs subagents, and **every one of those steps re-sends the
whole prefix**. Three consecutive turns of one real task:

| turn | request | API calls | context | cache read |
|---|---|---:|---:|---:|
| 1 | implement the fix | 106 | 57k → 232k | 15.7M |
| 2 | three follow-up tweaks | 64 | 226k → 313k | 17.0M |
| 3 | "готово" → merge and close | 22 | 63k → 108k | 1.9M |

Turn 2 did **half the work of turn 1 for 19 % more money**, purely because it
started on a 226k context: 85 % of its 17M cache-read was re-reading turn 1.
Per API call it cost 40k tokens against turn 1's 20k. Nothing about the request
was expensive — the *seat* was.

That is why turn length multiplies the read side and not the write side: the
prefix is re-written at most once per turn, and re-read on every call.

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

> These six figures predate the dedupe fix of 2026-08-10 and are overstated by
> roughly a third: one API response is written to the transcript once per
> content block, and the scan was summing each of them. A measured session read
> as 261 calls / $38.69 when it was 170 calls / $26.23. The proportions — and
> therefore the argument — are unchanged.

## The decision

Everything the decision needs is free and known *before* the turn runs:
`ctx_tokens` from the previous turn's `usage` block, idle time from the clock,
and turn length from the running average of turns already observed. So there is
no need to predict whether a particular turn will hit the cache — only to price
it:

```
E[$] = ctx_tokens × ( calls × 0.1  +  p(miss | idle) × (2.0 − 0.1) )
                   × PRICE_IN_PER_MTOK / 1e6
        └ every call re-reads ┘   └ only the first one can miss ┘
```

Rotate when that exceeds `ROTATE_TURN_COST_USD`, or when `ctx_tokens` passes
`ROTATE_HARD_CTX` regardless of price (the context window is a wall, not a
budget).

At the default $3.00 and a 45-call turn that works out to:

| idle | rotates at context ≥ |
|---|---:|
| < 10 min | ~130k |
| 10–30 min | ~123k |
| 30–60 min | ~104k |
| > 60 min | ~102k |

Idle time still moves the number, but it no longer decides it — **context size
does**, which is the point. A handoff costs ~7k tokens to carry plus about five
calls of re-orientation, so anything above ~100k pays that back several times
over within a single turn.

Both inputs are **self-calibrating** and live in `state/cache_stats.json`:
`p(miss | idle)` records the outcome of every turn seeded with the table above,
and `calls_per_turn` is an EWMA (α = 0.25, clamped to 5–200) over how long turns
actually run here. A mean would be wrong — turn length swings by request type,
and what matters is what the *next* one is likely to look like.

### Why the old threshold was wrong

Until 2026-08-10 the formula priced a **single API call** and called it a turn:

```
E[$] = ctx_tokens × ( p × 2.0 + (1 − p) × 0.1 ) × PRICE_IN / 1e6
```

At 45 calls a turn that under-reads a warm fat session by ~45×, and its rotate
points (~1.3M warm, ~150k cold) meant a live chat effectively never rotated. On
the task measured above it let turn 2 start on a 226k context — a turn the
corrected model prices at $5.68, well over the threshold — and only fired
before turn 3, one turn too late. Retiring one turn earlier would have cost
~29 % less across the task.

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
| `manual` | never rotates; advises `/new` once per session when the threshold trips |
| `auto` | rotates, and says so in the same message |

**Ship on `manual`.** Watch the advisories for a few days, confirm they fire
where you would have wanted them to, then switch to `auto`.

### One turn, one message

Neither mode posts a message of its own. Both fold their line into the ack the
agent is about to post anyway (the `{session_stats}` block), because the two
used to arrive seconds apart saying overlapping things — and the advisory read
as a decision the receiver had not actually made:

```
Продолжаю в ветке task-app-languages-fr-de-es      ← the agent's own words
⚙️ Начал новую сессию — пауза 9 д 13 ч, кеш почти наверняка мёртв. Продолжаю в ней.
прошлая <session-uuid> — контекст 395 181 · потрачено ≈ $88.01 · следующий ход на ней стоил бы ≈ $11.44 при пороге $3.00
session: <new-session-uuid>  (2-я в цепочке)
```

The three numbers that justify the decision — context, spend, threshold — are
in there; the retired session's full breakdown (per-kind spend, cache misses,
the `claude --resume` line) is one `/sessions` away and no longer clutters the
chat. Costs are reported when the task is *taken on*, so they are the estimate
for the turn ahead; what the turn actually cost lands in the next `/status`.

In `manual` mode the same slot carries the advice instead, once per session:

```
⚙️ Контекст 495 281 токенов, следующий ход ≈ $14.35 при пороге $3.00 — пора начать новую сессию: /new
```

Nothing is deleted — every retired session is still openable locally with
`claude --resume`, and `/sessions` lists the whole chain.

## When it deliberately does nothing

- **A turn was interrupted.** A mid-flight redirect resumes the same session;
  continuity matters more than the saving. An explicit `/new` still wins.
- **No transcript on disk.** Context size is unknown, so nothing is estimated
  and nothing rotates; `/status` says so.
- **Below `ROTATE_MIN_CTX`.** Small sessions are never worth resetting — under
  ~60k the handoff plus re-orientation costs more than staying put.
- **The chat is hot.** Being warm still buys ~28k of extra context over a cold
  chat (130k vs 102k), but it no longer buys an unbounded session: past ~130k
  even a perfectly cached conversation rotates, because the read side is paid
  on every call of the turn.

## Configuration

See the `Session rotation`, `Handoff` and `Pricing` blocks in `.env.example`.
The two you will actually touch:

```bash
SESSION_ROTATE_MODE=manual     # -> auto once you trust it
ROTATE_TURN_COST_USD=3.00      # lower = smaller contexts, more resets
ROTATE_CALLS_PER_TURN=45       # seed only; replaced by the measured average
```

`/status` prints the live turn length next to the threshold, so a rotate point
that looks wrong can be traced to the estimator rather than guessed at.

Prices default to Anthropic API list rates. On a subscription the absolute
figures are notional; the hit/miss ratio that drives the decision is unchanged.
