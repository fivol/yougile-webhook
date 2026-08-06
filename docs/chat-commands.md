# Chat commands

Slash commands typed into a YouGile task chat are answered by the receiver
itself. No agent is spawned, no tokens are spent, the status sticker is not
touched, and `last_message_id` does not move. Asking `/status` is free, which is
the point — the question "is this chat about to get expensive?" should not cost
anything to ask.

They work with or without the `@Agent` prefix, and are matched against the
message text with tags stripped, so YouGile's HTML wrapping does not matter.

## The commands

| Command | What it does |
|---|---|
| `/status` | Context size, cache outcome of the last turn, spend, workspace, and whether the next turn will start a new session |
| `/sessions` | The chain of sessions for this task — ids, spend, why each was retired |
| `/new [reason]` | Force the next turn into a fresh session |
| `/keep` | Cancel a pending auto-rotation, once |
| `/stop` | Interrupt the running turn |
| `/help` | The list above, in chat |

`@Agent /new <instruction>` combines the two: rotate, then act on the
instruction in the new session.

## `/status`

```
⚙️ Статус

Сессия
  id            <session-uuid>   (1-я из 1 в цепочке)
  контекст      395 181 токенов · 360 запросов · последний 9 д 13 ч назад
  прошлый ход   ✅ попал в кеш (read 393 584, write 1 595) — факт $0.23
  за сессию     ≈ $88.01   (запись $39.46 · чтение $41.18 · вывод $7.34)

Ротация
  режим         auto · порог $1.50 за ход · потолок 600 000 токенов
  сейчас        следующий ход ≈ $3.76 → начнётся НОВАЯ сессия
  промах кеша   0-10 мин 9% · 10-30 мин 26% · 30-60 мин 67% · 60+ мин 95%

Воркспейс  /Users/…/worktrees/add-csv-export
  web        task-add-csv-export → dev   https://github.com/org/repo/pull/123
  api        task-add-csv-export → dev   https://github.com/org/repo/pull/124

Чат
  обработано до 1785410086928
  агент: свободен
```

Every number is read locally: the context, cache outcome and spend come from
claude's own transcript, the workspace from `.feature.json`, the miss
probabilities from `state/cache_stats.json` (which the receiver keeps
calibrated from its own history).

`запросов` counts API calls, not conversation turns — one `@Agent` message
typically drives dozens of them.

Sums are estimates at API list price (`PRICE_*` in `.env`). On a subscription
they are notional but comparable to each other.

## Stats on the agent's ack

Independently of the commands, the ack the agent posts when it picks up work
(`Исследую` / `Начал работу` / `Продолжаю в ветке …`) carries the same
accounting:

```
Продолжаю в ветке task-add-csv-export
session: <session-uuid>
контекст 312 400 · этот ход ≈ $0.87 · за сессию ≈ $41.20
```

`этот ход ≈` is the estimate made *before* the turn ran; `/status` shows what
it actually cost afterwards. The receiver renders this block and the prompt
instructs the agent to paste it verbatim — the same mechanism that has always
carried the `session:` line. Disable with `ACK_STATS=false`.

## Who may run them

`COMMAND_ALLOWED_EMAILS` if set; otherwise the union of every rule's
`allowed_sender_emails`, so commands inherit the same allowlist as `@Agent`
itself. A command from anyone else is logged and refused in the chat — never
silently dropped.

The sender id is read with `_deep_get(payload, SENDER_KEYS)`, exactly like the
rules read it. This matters: a `chat_message-created` payload carries
`fromUserId` at the **top level**, as a sibling of `payload`, not inside it.
Reading one fixed path instead made every command look like it came from an
unknown user, and the owner's own `/status` was dropped without a word.

## Keeping them out of the agent's context

Command messages and the receiver's replies to them are plumbing, not
conversation. Both are filtered out of the history the agent sees — commands by
the same pattern that matches them, replies by their `COMMAND_REPLY_PREFIX`
(`⚙️` by default). So a chat full of `/status` checks does not dilute what the
agent reads.

## Disabling

`COMMANDS_ENABLED=false` turns the dispatcher off entirely; messages starting
with a slash then fall through to normal rule matching.
