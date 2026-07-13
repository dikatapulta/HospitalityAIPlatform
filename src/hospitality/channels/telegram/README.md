# channels/telegram — канал Telegram (Task 0016)

Первый реальный вход гостя в систему: приём вебхука Telegram Bot API, нормализация
в общий контракт канала, идемпотентная запись диалога, отправка ответов. Канал —
композиционный слой (§5.1), **не порт ядра** (§8): обязательного Fake-адаптера нет,
в тестах он воспроизводится payload'ами вебхука.

> **Граница задачи (PHASE0):** Task 0016 доводит канал до сохранённого `Message` и
> ответа гостю. Вызов оркестратора (Task 0015) и превращение сообщения в заявку
> подключает **Task 0017** («сквозная сборка»). Оркестратор уже спроектирован под
> этот канал: он ждёт, что вызывающая сторона (этот модуль) хранит историю диалога
> и `pending_action` между ходами — задел для 0017.

## Контракт нормализованного сообщения (`channels/base.py`, P-7)

`NormalizedMessage` — единственный формат, который канал отдаёт наверх; выше по
стеку неизвестно, из какого канала пришло сообщение. Зарезервировано поле
`reply_to` (ответ на конкретное сообщение): Telegram присылает полный объект —
заполняется сразу; WhatsApp (Phase 1) даст только id и восстановит текст по
`external_message_id` из истории (DISCUSSION_LOG «Контракт … reply-to»).

## Поток обработки (`service.process_update`)

1. **Проверка секрета** (`router.verify_telegram_secret`, §8.4): Telegram шлёт
   `secret_token` из `setWebhook` в заголовке `X-Telegram-Bot-Api-Secret-Token`.
   Неверный/пустой секрет → **403 `ERR-TELEGRAM-001`**, до разбора тела. Пустой
   `TELEGRAM_WEBHOOK_SECRET` = вебхук закрыт (fail-closed, §11).
2. **Нормализация** (`normalize.py`): не-сообщение (edited_message и т.п.) → no-op
   200; текст → `TEXT`; не-текст (фото/голос/стикер) → `UNSUPPORTED`.
3. **Маппинг чата на тенанта**: Phase 0 — один бот = демо-тенант, по slug из
   `TELEGRAM_TENANT_SLUG` (как сервисный токен в `platform/auth.py`). Пер-чатовый
   маппинг (несколько отелей за одним ботом) — Phase 1.
4. **Идемпотентная запись** (`store.py`, P-8): диалог — по `(tenant, channel,
   chat_id)`; входящее — под уникальным ключом доставки `(tenant, idempotency_key)`.
   Повторный вебхук с тем же `update_id` не создаёт второй `Message` и не влечёт
   второй ответ.
5. **Ответ** (`client.py`): не-текст → вежливый отказ (best-effort отправка + запись
   исходящего `Message`). На текст в Task 0016 ответ не шлётся (AI — Task 0017).

## Файлы

| Файл | Что даёт |
| --- | --- |
| `../base.py` | `NormalizedMessage`, `MessageKind`, `ReplyTo` — контракт всех каналов (P-7) |
| `schemas.py` | Подмножество payload Telegram Bot API (`TelegramUpdate`, …), `extra="ignore"` |
| `normalize.py` | `TelegramUpdate` → `NormalizedMessage` (чистая функция) |
| `models.py` | `Conversation`, `Message` — тенантные таблицы (канон RLS Task 0009) |
| `store.py` | Идемпотентная запись диалога (P-8) |
| `client.py` | `TelegramSender` (порт отправки) + боевая реализация на httpx |
| `service.py` | `process_update` — оркестрация приёма, маппинг чата на тенанта |
| `router.py` | Вебхук `POST /channels/telegram/webhook` + проверка секрета (§8.4) |

## Таблицы (миграция `0008`, RLS — копия канона `0002`)

- `conversations` — `id`, `tenant_id` (FK+индекс), `channel`, `external_id`
  (уникальны в тройке с `tenant_id`, `channel`), `created_at`, `updated_at`.
- `messages` — `id`, `tenant_id` (FK+индекс), `conversation_id` (FK+индекс),
  `direction` (inbound/outbound), `content_kind` (text/unsupported), `text`,
  `external_message_id`, `idempotency_key` (уникален в паре с `tenant_id`;
  NULL у исходящих), `correlation_id`, `created_at`.

Обе таблицы под RLS (ENABLE + FORCE + политика `tenant_isolation`); изоляция
покрыта тестами (`tests/test_tenant_isolation.py`).

## Аутентификация вебхука

Вебхук **не** использует сервисный токен `/api/v1/*` (Task 0013) и зависимость
`require_authenticated_tenant`: он аутентифицируется секретом вебхука Telegram
(§8.4), а тенанта ставит сам по маппингу чата. Роутер подключает composition root
(`hospitality/app.py`) отдельно от роутеров API.

## Конфигурация окружения (`shared/config.py`)

- `TELEGRAM_WEBHOOK_SECRET` — секрет вебхука (пустой = закрыт).
- `TELEGRAM_BOT_TOKEN` — токен бота для отправки ответов (пустой валиден для тестов).
- `TELEGRAM_TENANT_SLUG` — маппинг чата на тенанта (по умолчанию `demo-hotel`).
- `TELEGRAM_API_BASE_URL` — база Bot API (по умолчанию `https://api.telegram.org`).

Регистрация вебхука и секретов на staging — `docs/runbooks/telegram.md`.

## Зависимости

Внутренние: kernel — `hospitality.shared` (db, tenancy, errors, logging, config,
middleware) и `hospitality.platform.models` (реестр тенантов для маппинга чата).
Доменные модули не импортирует (в Task 0016). Внешние: `httpx` (исходящие вызовы
Bot API). Импортируется только composition root'ом (`router`) и `alembic/env.py`
(`models`).

## Типовые сценарии изменения

- **Новый тип входящего (фото/голос с разбором)** — тип в `MessageKind` +
  обработка в `normalize.py`/`service.py` + тесты; таблицы не трогаются.
- **Ответы гостю на текст (AI)** — Task 0017: `process_update` зовёт
  `ai.orchestrator.handle_message`, хранит историю (`messages`) и `pending_action`
  в состоянии диалога, шлёт `reply_text` через `client.py`.
- **Несколько отелей за одним ботом** — заменить маппинг по slug на пер-чатовую
  таблицу привязки (`_resolve_tenant` в `service.py`); Phase 1.
- **Новый канал (WhatsApp/Email)** — новый пакет `channels/<name>` по этому образцу;
  контракт `NormalizedMessage` не меняется, WhatsApp лишь реализует сборку
  `reply_to.text` по `external_message_id` из истории.
