# yougile-webhook

Приёмник вебхуков YouGile, который при срабатывании триггера запускает Claude Code
(`claude --dangerously-skip-permissions`) в заданной рабочей директории — фактически
«бот в задаче»: пишешь `@Agent ...` в чат — на ноуте поднимается claude и делает работу.

```
YouGile  ──POST──▶  https://<your-public-domain>/yougile/webhook
                    │
                    ▼  (любой способ публикации локального порта: FRP / cloudflared / ngrok / nginx на VPS)
                    ▼
                    127.0.0.1:8080 (опц. nginx, если нужно разводить пути на несколько локальных сервисов)
                                                  │
                                                  ▼
                                       /yougile/webhook → 127.0.0.1:9100 (этот сервис)
                                                  │
                                                  ▼ если триггер сработал
                                       claude --dangerously-skip-permissions
                                       cwd=$CLAUDE_WORKDIR
```

## Файлы

| Путь | Что |
|---|---|
| `app.py` | FastAPI: принимает вебхук, фильтрует, спавнит claude |
| `run.sh` | Запуск uvicorn (читает `.env`) |
| `register_webhook.py` | CLI для регистрации/просмотра/удаления подписки в YouGile |
| `prompt_template.txt` | Шаблон промпта для claude (`{event_json}` — плейсхолдер) |
| `.env` / `.env.example` | Вся конфигурация |
| `logs/events.jsonl` | Полные входящие вебхуки |
| `logs/claude.log` | stdout/stderr запущенных claude-процессов |
| `logs/uvicorn.{log,err}` | Логи uvicorn (пишутся launchd) |

Системные конфиги (для macOS, опциональны):
| Путь | Что |
|---|---|
| `/opt/homebrew/etc/nginx/servers/*.conf` | nginx-развилка, если на 8080 несколько сервисов |
| `~/Library/LaunchAgents/org.local.yougile-webhook.plist` | launchd-автозапуск uvicorn (есть пример ниже) |

Публикация во вне (`https://your-domain/yougile/webhook` → `127.0.0.1:9100`) — на ваш выбор:
FRP, cloudflared, ngrok, обратный прокси на отдельном VPS и т.д.

## Конфигурация (`.env`)

Всё руководится переменными окружения. Полный список с дефолтами — в `.env.example`.

Ключевые:

- `YOUGILE_API_KEY` — ключ YouGile (используется только `register_webhook.py` и ресолвом email→UUID на старте)
- `YOUGILE_WEBHOOK_URL` — публичный URL приёмника
- `YOUGILE_WEBHOOK_EVENT` — событие подписки (например `chat_message-created`, `task-*`, `.*`)
- `CLAUDE_TRIGGER_EVENTS` — какие события поднимают claude (csv, пусто = все из подписки)
- `CLAUDE_TRIGGER_PATTERN` — регексп по сериализованному JSON пейлоада (пусто = не фильтровать)
- `ALLOWED_USER_EMAILS` — белый список отправителей. На старте резолвится в UUID через `/api-v2/users` (одним запросом, потом в памяти)
- `SENDER_KEYS` — какие ключи payload считать «id отправителя» (ищется и на верхнем уровне, и в `data`/`payload`/`message`/`object`)
- `CLAUDE_BIN`, `CLAUDE_WORKDIR`, `CLAUDE_EXTRA_ARGS` — что и где запускать
- `CLAUDE_PROMPT_TEMPLATE_FILE` — путь к шаблону промпта
- `CLAUDE_MAX_CONCURRENT` — лимит одновременных claude-процессов (хард-кап, лишние события дропаются с warning)

После правки `.env` — перезапустить:

```bash
launchctl unload  ~/Library/LaunchAgents/org.local.yougile-webhook.plist
launchctl load -w ~/Library/LaunchAgents/org.local.yougile-webhook.plist
```

## Установка

```bash
git clone <this-repo>
cd yougile-webhook
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env  # затем отредактировать
```

## Запуск вручную

```bash
./run.sh
curl http://127.0.0.1:9100/healthz
```

`/healthz` отдаёт текущие настройки (включая разрезолвленные `allowed_user_ids`).

## Регистрация подписки в YouGile

```bash
.venv/bin/python register_webhook.py list                # текущие подписки
.venv/bin/python register_webhook.py create              # создать (по настройкам из .env)
.venv/bin/python register_webhook.py delete <hook-id>    # удалить
```

## Триггер-логика

Событие запускает claude **только если выполнены ВСЕ условия**:

1. `event` входит в `CLAUDE_TRIGGER_EVENTS` (если список не пуст)
2. JSON пейлоада соответствует `CLAUDE_TRIGGER_PATTERN` (если задан)
3. `extract_sender_id(payload)` ∈ разрезолвленных `ALLOWED_USER_IDS` (если whitelist не пуст)

Ответ `/yougile/webhook`:

```json
{"ok": true, "claude_triggered": true, "reason": "ok"}
```

или `"reason": "sender-not-allowed:<uuid>"` / `"pattern-no-match"` / `"event-not-in-..."` — удобно дебажить из логов.

## Реальный пейлоад от YouGile (chat_message-created)

```json
{
  "event": "chat_message-created",
  "fromUserId": "1e2b1336-...",
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

`chatId` совпадает с UUID задачи — это то, что нужно передавать в `send_task_message` как `taskId`.

## Автозапуск

LaunchAgent `~/Library/LaunchAgents/org.local.yougile-webhook.plist`:

- `RunAtLoad=true`, `KeepAlive=true` — стартует при логине и поднимается обратно при падении
- stdout/stderr → `logs/uvicorn.{log,err}`
- `PATH` в `EnvironmentVariables` должен включать директорию, где лежит claude CLI (например `~/.local/bin`), иначе спавнящемуся процессу его не найти

Управление:
```bash
launchctl list | grep yougile-webhook
launchctl unload  ~/Library/LaunchAgents/org.local.yougile-webhook.plist
launchctl load -w ~/Library/LaunchAgents/org.local.yougile-webhook.plist
```

## Безопасность

- `.env` содержит API-ключ YouGile в открытом виде — не коммитить (есть в `.gitignore`).
- `--dangerously-skip-permissions` отключает любые подтверждения у claude. Whitelist по `ALLOWED_USER_EMAILS` — единственный заслон от того, чтобы случайный участник чата запустил у вас shell-команды. Держите список минимальным.
- Если используете туннель (FRP/cloudflared/ngrok) — его токен это отдельный секрет, храните вне репозитория.

## Известные хвосты

- При старте uvicorn разово стучится в `/api-v2/users` для резолва email→UUID. Если YouGile API недоступен — фильтр по отправителю отключается (с warning в логе). Можно вынести в фоновое обновление при необходимости.
- При смене whitelist нужно перезапускать сервис (UUID кешируется в памяти процесса).
- Параллелизм: при превышении `CLAUDE_MAX_CONCURRENT` событие дропается, очереди нет.
