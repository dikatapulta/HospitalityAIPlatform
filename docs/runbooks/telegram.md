# Runbook — канал Telegram: бот, вебхук, сквозной скелет (Task 0016, 0017)

> FOUNDATION §8.4 (секреты вебхуков), §11 (секреты — только в `.env`/секрет-хранилище).
> Это ручные шаги основателя (как генерация секретов): код канала готов, но чтобы
> сообщение реального гостя дошло, нужен зарегистрированный бот и вебхук.

## Что должно получиться

**Task 0016 (вход):** гость пишет боту → Telegram шлёт `POST` на
`https://<staging>/channels/telegram/webhook` с заголовком-секретом → приложение
проверяет секрет, сохраняет `Message`, на не-текст отвечает вежливым отказом.

**Task 0017 (сквозной скелет):** на **текст** гость получает ответ AI-консьержа;
просьба «уберите номер» → заявка → в **staff-чат** приходит уведомление → сотрудник
командой `/assign · /start · /done <id>` закрывает заявку → гость получает
подтверждение. Весь путь «гость → заявка → уведомление службе» связан одним
correlation_id в логах (outbox протаскивает его через доставку события).

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
TELEGRAM_STAFF_CHAT_ID=<chat.id staff-чата>      # см. шаг 2а (Task 0017)
```

Применить: `deploy.sh` (или пере-деплой из `main`) пересоздаёт контейнеры app **и
worker** с новым `.env` (уведомления шлёт worker — ему тоже нужны `TELEGRAM_BOT_TOKEN`
и `TELEGRAM_STAFF_CHAT_ID`, они уже проброшены в `docker-compose.staging.yml`).
Демо-тенант `demo-hotel` и его категории заявок засеяны деплоем (`make seed`).

## Шаг 2а. Узнать chat.id staff-чата (Task 0017)

Staff-чат — куда бот шлёт уведомления о заявках и где персонал их закрывает.
Проще всего — личный чат с ботом или отдельная группа:

1. Напишите боту (или добавьте его в группу и напишите там любое сообщение).
2. Запросите обновления и возьмите `message.chat.id`:
   ```bash
   curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getUpdates" \
     | python3 -c "import sys,json; print([u['message']['chat']['id'] for u in json.load(sys.stdin)['result'] if 'message' in u])"
   ```
   Для группы id отрицательный (например `-1001234567890`) — это нормально, кладите
   как есть в `TELEGRAM_STAFF_CHAT_ID`. Для демо staff- и гостевой чат могут быть
   разными личными чатами (нужен второй Telegram-аккаунт) или гостевым сделать один
   чат, а staff — группу с ботом.

## Шаг 3. Постоянный HTTPS-вход (issue #65)

Telegram шлёт вебхуки **только на HTTPS** и только на порты **443, 80, 88, 8443**.
Приложение слушает `:8000` по HTTP внутри compose-сети — наружу порт не открыт.

Вход даёт **именованный Cloudflare-туннель** (сервис `cloudflared` в
`docker-compose.staging.yml`): держит исходящее соединение к Cloudflare, TLS
терминирует Cloudflare, публичный адрес — `https://staging.necturn.com`. Порт
приложения в интернет не выставляется (закрывает утечку Swagger по HTTP, снимает
нужду в правке ufw). Разовая настройка туннеля (create/route/creds) — в
[deploy.md](deploy.md); id туннеля и ingress — в `ops/deploy/cloudflared/config.yml`.

> Ручной `cloudflared tunnel --url http://localhost:8000` (случайный адрес)
> использовался при приёмке DoD Task 0017 и **умирал при перезапуске** — вебхук
> терялся молча. Именованный туннель как сервис это чинит.

## Шаг 4. Регистрация вебхука — автоматически в deploy.sh

`setWebhook` больше **не делается руками**: `deploy.sh` регистрирует вебхук на
`PUBLIC_BASE_URL/channels/telegram/webhook` на каждом деплое и тут же проверяет
`getWebhookInfo` (адрес совпал → иначе деплой падает). Это ловит обрыв входа,
которого не видел `make smoke`. Секрет `secret_token` — тот же
`TELEGRAM_WEBHOOK_SECRET`, что проверяет `router.py` (fail-closed).

Ручная перерегистрация (диагностика) — тем же вызовом, что и deploy.sh; **URL с
токеном целиком не логировать** (§11):

```bash
BOT_TOKEN='123456:ABC-...'
SECRET='<то же, что TELEGRAM_WEBHOOK_SECRET>'
URL='https://staging.necturn.com/channels/telegram/webhook'

curl -sS "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
  --data-urlencode "url=${URL}" \
  --data-urlencode "secret_token=${SECRET}"
```

Проверить регистрацию: `curl "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo"`
— поля `url`, `pending_update_count`, `last_error_message`.

## Шаг 5. Проверить DoD Task 0016 (вход)

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

## Шаг 6. Проверить DoD Task 0017 (сквозной скелет)

Нужны заданный `TELEGRAM_STAFF_CHAT_ID` и `ANTHROPIC_API_KEY` (боевая модель).

1. **Гость → заявка.** Из гостевого чата: «Уберите, пожалуйста, номер 305». Бот
   переспросит подтверждение (гейт P-9) — ответить «да». Бот подтвердит оформление.
   В `service_requests` появится строка `new`; в логах у гостевого запроса «да»
   запомнить `correlation_id` (событие `guest_turn_handled`, `service_request_created`).
2. **Уведомление службе.** В **staff-чат** в течение секунд придёт «🔔 Новая заявка …»
   с `id: <uuid>`. Убедиться, что уведомление несёт **тот же** correlation_id:
   ```bash
   docker compose ... logs worker | grep <correlation_id>   # staff_notified
   ```
   Это и есть ключевая проверка DoD: один correlation_id от сообщения гостя до
   уведомления службе, через async-границу outbox.
3. **Закрытие персоналом.** В staff-чате ввести по очереди (id — из уведомления):
   `/assign <id>` → `/start <id>` → `/done <id>`. Бот на каждую команду отвечает
   новым статусом; недопустимый порядок отвергается понятной ошибкой.
4. **Подтверждение гостю.** После `/done` в **гостевой** чат придёт «Ваша заявка …
   выполнена». В `service_requests` заявка в статусе `done`.

Диагностика сквозного потока — по `correlation_id` в логах `app` (приём, оркестратор)
и `worker` (доставка событий, уведомления). Категории ошибок оркестратора/инструмента
— `docs/runbooks/errors.md` (ERR-AI-00x).

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

Кабинета персонала нет — заявки закрываются командами в staff-чате (`staff.py`),
RBAC нет: любой в staff-чате может закрыть заявку (ADR-011, §17.7 — RBAC v1 в Phase 1).
Один бот обслуживает демо-тенанта (маппинг по `TELEGRAM_TENANT_SLUG`); несколько
отелей за одним ботом — Phase 1. Обратная адресация «заявка → чат гостя» держится на
`request_origins` (ADR-011); с модулем `guests/` (Phase 1) переедет на идентичность
гостя.
