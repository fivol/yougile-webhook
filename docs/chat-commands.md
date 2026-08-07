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

### Commands that carry an instruction

`/new` and `/keep` only decide what the **next** turn does, so they can be
prefixed to an ordinary request:

```
@Agent /new доделай (подними бек, настрой энвы)
```

That arms the rotation **and** runs the turn — the trailing text is treated as
the instruction, not as a `[reason]` label. The receiver posts no `⚙️` reply of
its own in that case: the agent is about to post its ack, and the "started a
new session" line rides on it, so the chat still sees one message per turn.

The other commands (`/status`, `/sessions`, `/stop`, `/help`) never fall
through — they answer and stop, whatever follows them.

> Without this, `@Agent /new доделай …` was consumed as a bare `/new` with
> `доделай …` echoed back as the rotation *reason*: the flag was armed, the
> chat was told "принято", and no agent was ever started. The message also
> looked like a slash command to the history filter, so even a later turn would
> not have seen the instruction.

## `/status`

Four blocks, in the order the questions get asked — what this session is, what
happens next, where the work lives, housekeeping:

```
⚙️ Статус — агент свободен            (heading)

Сессия 2-я из 2 · 65c97084-…          (bold label, id in <code>)
598 522 токена · 623 хода · последний 9 мин назад
✅ прошлый ход попал в кеш — $0.34
за сессию ≈ $147.40 — запись $21.84 · чтение $110.18 · вывод $15.37
за всю задачу ≈ $154.60 по цепочке из 2 сессий

Дальше
следующий ход ≈ $0.77 → продолжу текущую
после часа простоя ≈ $5.70 → и тогда новая
порог $1.50 за ход · потолок 600 000 токенов · режим auto
кеш стынет: 9% за 10 мин · 26% за полчаса · 67% за час · 95% дальше

Воркспейс app-languages-fr-de-es · ветка task-app-languages-fr-de-es
 • app → dev · PR 161                 (a <ul>, PRs are <a> links)
 • auth → main · PR 19
 • bitfront → dev · PR 317 · превью

обработано до сообщения 1786013187228
```

It is sent as HTML (`<h4>`, `<b>`, `<code>`, `<ul>`, `<a>`) with a plain-text
twin for notifications. It used to be a `<pre>` block of aligned columns, which
the narrow chat panel wrapped into ribbons — hence short lines joined by `·`,
and never a column of padding.

Every number is read locally: the context, cache outcome and spend come from
claude's own transcript, the workspace from `.feature.json`, the miss
probabilities from `state/cache_stats.json` (which the receiver keeps
calibrated from its own history).

`ход` counts an API call, not a conversation turn — one `@Agent` message
typically drives dozens of them.

Sums are estimates at API list price (`PRICE_*` in `.env`). On a subscription
they are notional but comparable to each other.

## Stats on the agent's ack

Independently of the commands, the ack the agent posts when it picks up work
(`Исследую` / `Начал работу` / `Продолжаю в ветке …`) carries the same
accounting:

```
Продолжаю в ветке task-add-csv-export
сессия <session-uuid> · 2-я в цепочке
контекст 312 400 · этот ход ≈ $0.87 · за сессию ≈ $41.20
```

`этот ход ≈` is the estimate made *before* the turn ran; `/status` shows what
it actually cost afterwards. The receiver renders this block and the prompt
instructs the agent to paste it verbatim — the same mechanism that has always
carried the session id. Disable with `ACK_STATS=false`.

Unlike the command replies, this block is **plain text with no markup at all**:
the agent pastes it into `text` as well as `textHtml`, and a stray `**` or
`<b>` would show up as literal characters there. Same for the rotation and
advisory lines that ride in it.

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
