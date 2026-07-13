# Runbook — канал Telegram: бот и вебхук (Task 0016)

> FOUNDATION §8.4 (секреты вебхуков), §11 (секреты — только в `.env`/секрет-хранилище).
> Это ручные шаги основателя (как генерация секретов): код канала готов, но чтобы
> сообщение реального гостя дошло, нужен зарегистрированный бот и вебхук.

## Что должно получиться

Гость пишет боту в Telegram → Telegram шлёт `POST` на
`https://<staging>/channels/telegram/webhook` с заголовком-секретом → приложение
проверяет секрет, сохраняет `Message` в БД (виден по correlation_id в логах),
на не-текст отвечает вежливым отказом.

## Шаг 1. Создать бота (BotFather)

1. В Telegram открыть [@BotFather](https://t.me/BotFather) → `/newbot` → задать имя
   и username (заканчивается на `bot`).
2. BotFather выдаёт **токен** вида `123456:ABC-...`. Это `TELEGRAM_BOT_TOKEN` —
   секрет, в репозиторий не коммитится (docs/runbooks/secrets.md).

## Шаг 2. Прописать секреты на сервере

В `/opt/hospitality/.env` (шаблон — `ops/deploy/.env.staging.example`):

```dotenv
TELEGRAM_BOT_TOKEN=123456:ABC-...        # токен из шага 1
TELEGRAM_WEBHOOK_SECRET=<openssl rand -hex 32>   # придумать секрет вебхука
TELEGRAM_TENANT_SLUG=demo-hotel
```

Применить: `deploy.sh` (или пере-деплой из `main`) пересоздаёт контейнер app с
новым `.env`. Демо-тенант `demo-hotel` уже засеян деплоем (`make seed`).

## Шаг 3. Требование HTTPS

Telegram шлёт вебхуки **только на HTTPS** и только на порты **443, 80, 88, 8443**.
Приложение на staging слушает `:8000` по HTTP — напрямую Telegram на него не пойдёт.
Варианты:

- **Быстрый (для проверки DoD):** туннель до `:8000` с TLS —
  `cloudflared tunnel --url http://localhost:8000` или `ngrok http 8000`; берётся
  выданный `https://…`-URL.
- **Постоянный:** reverse-proxy (Caddy/nginx) с TLS-сертификатом (Let's Encrypt)
  перед приложением на 443 (задача инфраструктуры Phase 1; сюда же ляжет и HTTPS
  для `/api/v1/*`).

## Шаг 4. Зарегистрировать вебхук

Передать Telegram URL и **тот же** секрет, что в `.env` (`secret_token`):

```bash
BOT_TOKEN='123456:ABC-...'
SECRET='<то же, что TELEGRAM_WEBHOOK_SECRET>'
URL='https://<staging-или-туннель>/channels/telegram/webhook'

curl -sS "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
  -d "url=${URL}" \
  -d "secret_token=${SECRET}"
```

Проверить регистрацию: `curl "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo"`
— поля `url` и `has_custom_certificate`, `pending_update_count`, `last_error_message`.

## Шаг 5. Проверить DoD

1. Написать боту любой **текст** из Telegram.
2. Убедиться, что появилась строка `Message` (владельцем схемы, см. deploy.md):
   ```sql
   SELECT direction, content_kind, text, correlation_id, created_at
   FROM messages ORDER BY created_at DESC LIMIT 5;
   ```
   Ожидаемо: строка `inbound`/`text` с непустым `correlation_id`.
3. Найти след запроса в логах по этому `correlation_id`
   (`docker compose ... logs app | grep <correlation_id>`) — событие
   `telegram_message_stored`.
4. Отправить боту **фото/стикер** → в чат придёт вежливый отказ, в `messages` —
   `inbound`/`unsupported` + `outbound`/`text`.

## Диагностика

- **Все апдейты → 403 (`ERR-TELEGRAM-001`), сообщений нет:** секрет в `setWebhook`
  не совпал с `TELEGRAM_WEBHOOK_SECRET` в `.env`, либо секрет пуст (вебхук закрыт).
  Переустановить вебхук с верным `secret_token`. См. errors.md → ERR-TELEGRAM-001.
- **`getWebhookInfo.last_error_message` про TLS/соединение:** не выполнен шаг 3
  (HTTPS) или URL недоступен снаружи.
- **Ответ на не-текст не приходит, но `Message` сохранён:** отправка best-effort —
  проверить `TELEGRAM_BOT_TOKEN` и событие `telegram_send_failed` в логах.
- **Смена секрета/токена:** после ротации в `.env` обязательно переустановить
  вебхук (шаг 4) — иначе Telegram шлёт старый секрет и всё уходит в 403.

## Границы Phase 0

Ответ на **текстовое** сообщение (AI-консьерж) подключает **Task 0017** — сейчас
текст только сохраняется. Один бот обслуживает демо-тенанта (маппинг по
`TELEGRAM_TENANT_SLUG`); несколько отелей за одним ботом — Phase 1.
