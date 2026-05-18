# yougile-webhook

Принимает вебхуки YouGile, прогоняет их через rule engine и при срабатывании
правила запускает Claude Code (`claude --dangerously-skip-permissions`) с
заданным промптом и рабочей директорией.

Базовый сценарий: пишешь `@Agent ...` в чат задачи — на ноуте поднимается
claude, делает работу, отвечает в том же чате через YouGile MCP. Дополнительные
сценарии (новая задача в "In progress" → ресерч-агент, и т.п.) описываются
правилами в `rules.toml`.

**Сессии «как у живого собеседника».** При повторном упоминании в той же
задаче агент продолжается в **той же claude-сессии** (`--session-id` / `--resume`),
помнит контекст прошлых ходов, а в промпт автоматически инжектится **история
чата** (полная — при первом срабатывании; только новые сообщения — далее).
Хранится это в `state/sessions.json` (gitignored).

```
YouGile  ──POST──▶  https://<your-public-domain>/yougile/webhook
                    │
                    ▼  (любой способ публикации локального порта: FRP / cloudflared / ngrok / nginx на VPS)
                    ▼
                    127.0.0.1:9100 (uvicorn, этот сервис)
                                │
                                ▼ ищет первое подходящее правило в rules.toml
                                ▼ если нашёл
                                ▼
                    claude --dangerously-skip-permissions -p "<prompt из rule.prompt_file>"
                    cwd = rule.workdir | $CLAUDE_WORKDIR
```

## Структура проекта

| Путь | Что |
|---|---|
| `app.py` | FastAPI приёмник + rule engine |
| `run.sh` | Запуск uvicorn (читает `.env`) |
| `register_webhook.py` | CLI для подписок YouGile |
| `rules.example.toml` | Шаблон правил (коммитится) |
| `rules.toml` | Реальные правила пользователя (**gitignored**) |
| `prompts/*.example.txt` | Шаблоны промптов (коммитятся) |
| `prompts/*.txt` | Реальные промпты пользователя (**gitignored**) |
| `.env.example` / `.env` | Глобальные настройки (`.env` gitignored) |
| `requirements.txt` | Зависимости (`fastapi`, `uvicorn[standard]`) |
| `logs/events.jsonl` | Полные входящие вебхуки |
| `logs/claude.log` | stdout/stderr запущенных claude-процессов |
| `logs/uvicorn.{log,err}` | Логи uvicorn (пишутся launchd) |
| `state/sessions.json` | Per-chat: `session_id` + `last_message_id` (**gitignored**) |

Системное (не в репо, по желанию):
- `~/Library/LaunchAgents/org.local.yougile-webhook.plist` — launchd-автозапуск (есть пример ниже)
- ваш способ публикации `https://your-domain/yougile/webhook → 127.0.0.1:9100`
  (FRP / cloudflared / ngrok / nginx на VPS)

## Установка

```bash
git clone <this-repo>
cd yougile-webhook
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env                                 # отредактировать
cp rules.example.toml rules.toml                     # отредактировать
cp prompts/chat_mention.example.txt    prompts/chat_mention.txt
cp prompts/task_in_progress.example.txt prompts/task_in_progress.txt
```

## Конфигурация: rules.toml

Это основной файл настройки логики. Список правил, оцениваются сверху вниз,
**первое совпавшее срабатывает** — поэтому ставьте более специфичные сверху.

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
enabled = false                                  # включаемый функционал
events = ["task-created", "task-moved", "task-updated"]
column_names = ["In progress"]
allowed_sender_emails = ["you@example.com"]
prompt_file = "prompts/task_in_progress.txt"
# workdir = "/path/to/company-root"              # по умолчанию = $CLAUDE_WORKDIR
# extra_args = "--dangerously-skip-permissions --model sonnet"
```

Поля правила:

| Поле | Что делает |
|---|---|
| `name` | Свободный идентификатор (виден в логах и `/healthz`) |
| `enabled` | true / false — мгновенное отключение |
| `events` | Список YouGile event-типов (`chat_message-created`, `task-moved`, `task-created`, `task-updated`, ...) |
| `pattern` | Опциональный регексп по сериализованному JSON пейлоада |
| `allowed_sender_emails` | Белый список email отправителей. Резолвится в UUID на старте через `/api-v2/users` |
| `column_names` | Список названий колонок (например `["In progress"]`). Резолвится в UUID на старте через `/api-v2/columns` |
| `prompt_file` | Путь к текстовому файлу с промптом. Плейсхолдер `{event_json}` подставляется автоматически |
| `workdir` | Рабочая директория для claude (override `CLAUDE_WORKDIR`) |
| `extra_args` | CLI-аргументы для claude (override `CLAUDE_EXTRA_ARGS`) |
| `column_transition_only` | bool, default `true`. Когда задан `column_names`: срабатывать только если задача реально перешла в колонку (`prevData.columnId != payload.columnId`), а не на каждое `task-updated` пока она там лежит |
| `session_per_chat` | bool, default `true`. Если включено — claude запускается с `--session-id` (первый раз) или `--resume` (далее), привязанный к UUID чата задачи. Агент сохраняет контекст между упоминаниями. Если выключено — каждый запуск stateless |

Все списки/условия комбинируются по И. Пустое поле = условие неактивно.

### Логика `task_in_progress` (пример)

Правило срабатывает на `task-created` / `task-moved` / `task-updated`, если
текущий `columnId` в пейлоаде = UUID колонки "In progress". Это покрывает оба
кейса:

- задача создана сразу в "In progress";
- задача перемещена в "In progress" из другой колонки.

`task-moved` от YouGile содержит `payload.columnId` = **новый** column, а
`prevData.columnId` = **старый**. По умолчанию правило стреляет только на
**реальный переход** (`column_transition_only = true`) — иначе оно бы
срабатывало на каждое `task-updated` (редактирование заголовка, описания,
ассайнов), пока задача лежит в In progress.

Промпт `prompts/task_in_progress.txt` — пользовательский. Идея:
агент запускается из корня компании (где лежат несколько репозиториев), решает
какие репо релевантны задаче, спавнит лёгкие sub-агенты на каждый репо для
анализа (`Agent` tool с `Explore`), синтезирует короткую рекомендацию и постит
её обратно в чат задачи. См. `prompts/task_in_progress.example.txt` как
отправную точку.

## Конфигурация: .env

Глобальные настройки. `.env.example` обобщённый, `.env` (gitignored) — реальный.

| Переменная | Назначение |
|---|---|
| `YOUGILE_API_KEY` | API-ключ YouGile. Нужен и для подписки, и для резолва email/column на старте |
| `YOUGILE_API_BASE` | По умолчанию `https://yougile.com/api-v2` |
| `YOUGILE_WEBHOOK_URL` | Внешний URL приёмника. Используется `register_webhook.py` |
| `YOUGILE_WEBHOOK_EVENT` | Event-фильтр **подписки** YouGile (регексп). Делайте широким: `(chat_message\|task)-.*` или `.*` — реальная фильтрация на стороне rule engine |
| `YOUGILE_WEBHOOK_CHAT_FILTER` | Опц. server-side regexp по тексту chat_message |
| `HOST` / `PORT` | Куда биндится uvicorn (по умолчанию `127.0.0.1:9100`) |
| `CLAUDE_BIN` | Путь к бинарнику claude |
| `CLAUDE_WORKDIR` | Дефолтная рабочая директория (правила могут переопределить) |
| `CLAUDE_EXTRA_ARGS` | Дефолтные CLI-аргументы claude |
| `CLAUDE_MAX_CONCURRENT` | Глобальный лимит одновременных claude (excess → drop с warning) |
| `RULES_FILE` | По умолчанию `rules.toml` |
| `SENDER_KEYS` | Какие ключи payload считать «id отправителя» |
| `COLUMN_KEYS` | Какие ключи payload считать «id колонки» |
| `CHAT_ID_KEYS` | Какие ключи payload считать «UUID чата/задачи» (по умолчанию `chatId,id`). Используется как ключ к сессии claude |
| `CHAT_HISTORY_FIRST_TIME_LIMIT` | Сколько последних сообщений тянуть в промпт при ПЕРВОМ срабатывании на чат (default 20) |
| `CHAT_HISTORY_DELTA_LIMIT` | Сколько НОВЫХ сообщений (с момента предыдущего ответа агента) тянуть на последующих (default 50) |

После правки `.env` или `rules.toml` — перезапуск:

```bash
launchctl unload  ~/Library/LaunchAgents/org.local.yougile-webhook.plist
launchctl load -w ~/Library/LaunchAgents/org.local.yougile-webhook.plist
# или вручную
pkill -f "uvicorn app:app" ; ./run.sh
```

## Регистрация подписки в YouGile

```bash
.venv/bin/python register_webhook.py list                # текущие
.venv/bin/python register_webhook.py create              # создать по .env
.venv/bin/python register_webhook.py delete <hook-id>    # удалить
```

Одна подписка с широким event-regex покрывает все правила сразу.

## Запуск вручную

```bash
./run.sh
curl http://127.0.0.1:9100/healthz
```

`/healthz` отдаёт текущее состояние и резолв правил (UUID отправителей и колонок).

## Автозапуск (macOS, launchd)

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
    <key>ThrottleInterval</key><integer>10</integer>
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

`PATH` должен содержать директорию с `claude` CLI — иначе спавнящемуся
процессу его не найти.

```bash
launchctl load -w ~/Library/LaunchAgents/org.local.yougile-webhook.plist
launchctl list | grep yougile-webhook
```

## Реальные пейлоады YouGile (для справки)

`chat_message-created`:
```json
{
  "event": "chat_message-created",
  "fromUserId": "<user-uuid>",
  "payload": {
    "id": 1779102737006,
    "chatId": "<task-uuid>",       // == taskId, использовать для ответа
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

`task-moved`: то же, что `task-created`, плюс `prevData.columnId` со старой
колонкой. `payload.columnId` — новая колонка.

## Безопасность

- `.env` и `rules.toml` содержат API-ключ и почты соответственно — gitignored.
- `--dangerously-skip-permissions` снимает с claude любые подтверждения. Поэтому
  `allowed_sender_emails` обязательны — это единственный барьер от того, чтобы
  случайный участник чата стартанул у вас shell-команды. Держите список
  минимальным.
- Промпты с реальной логикой workflow тоже в gitignore — там могут быть
  внутренние подсказки, ссылки, имена репо.
- Если используете туннель (FRP/cloudflared/ngrok), его токен — отдельный
  секрет.

## Сессии и история чата

Каждый чат задачи в YouGile (`chatId == taskId`) маппится на **один**
постоянный `session_id` claude. State:

```
state/sessions.json
{
  "<task-uuid>": {
    "session_id": "<claude-session-uuid>",
    "last_message_id": 1779100000000,
    "updated_at": "..."
  }
}
```

Поток на каждое срабатывание правила с `session_per_chat = true`:

1. Извлекаем `chatId` из пейлоада (порядок ключей: `CHAT_ID_KEYS`).
2. Берём `chat_lock` неблокирующе. Если claude уже работает по этому чату —
   событие **дропается** (см. `spawn_reason: chat-busy` в логах). Очереди нет.
3. Тянем сообщения из чата через `/api-v2/chats/<chatId>/messages`:
   - первый раз — последние `CHAT_HISTORY_FIRST_TIME_LIMIT` (с пагинацией);
   - далее — только новее `last_message_id` (параметр `since` сдвинут на +1
     чтобы исключить уже виденное).
4. Рендерим промпт. Плейсхолдер `{chat_history}` в шаблоне заменяется на
   `[ts] sender: text` построчно. Если плейсхолдера нет — блок дописывается
   в конец промпта.
5. Спавним claude:
   - первый раз: `claude -p --session-id <uuid> "..."` (uuid генерируется);
   - далее: `claude -p --resume <uuid> "..."`.
6. После завершения процесса сохраняем (или подтверждаем) `session_id` и
   обновляем `last_message_id` до максимального из подгруженной партии.

Если по какой-то причине state потерялся / claude-сессия удалена с диска —
следующий запуск пойдёт как первый, агент получит полную историю заново и
будет работать в новой сессии. Безопасный fallback.

Для правил, где сессия не нужна (например, разовые ресерч-агенты, которым не
нужна память между задачами), ставьте `session_per_chat = false`.

## Дизайн / расширяемость

Rule engine спроектирован так, чтобы добавить новые сценарии не трогая `app.py`:

- **Новый триггер** = новый `[[rules]]` в `rules.toml` + промпт-файл.
- **Новый тип события YouGile** — просто перечислить в `events`, при условии
  что соответствующая подписка ловит этот event-тип.
- **Новый ключ payload** для отправителя / колонки — добавить в `SENDER_KEYS`
  или `COLUMN_KEYS` в `.env`.
- **Новые поля фильтрации** (тег, проект, доска, и т.п.) — точечно добавить в
  `Rule` dataclass и `rule_matches()`.
- **Альтернативные действия** (не claude, а HTTP-вызов / shell-команда) —
  можно ввести поле `action` в правило и развести по типам в спавне.

Логика matching: per-rule условия AND, между правилами — first-match-wins.
Этого достаточно для большинства сценариев; если понадобится «все совпавшие»
— это правка в `find_matching_rule()` (несколько строк).

## Хвосты / ограничения

- При старте uvicorn однократно стучится в `/users` и `/columns` для резолва.
  Если API недоступен — relevant правила не сработают, в логах будет warning.
- При смене whitelist / колонок / промптов нужен рестарт сервиса (всё кешируется в памяти).
- Параллелизм: при превышении `CLAUDE_MAX_CONCURRENT` событие дропается, очереди нет.
- YouGile может слать одно и то же логическое действие как `task-moved`, так и
  как `task-updated` (зависит от способа перемещения). Поэтому правило
  типовое лучше регистрировать на `["task-created", "task-moved", "task-updated"]`
  с `column_transition_only = true` (по умолчанию). Лишних срабатываний не будет
  благодаря сравнению `prevData.columnId` с `payload.columnId`.
