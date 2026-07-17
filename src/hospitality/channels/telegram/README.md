# channels/telegram — канал Telegram (Task 0016, 0017)

Первый реальный вход гостя в систему и первый полный проход скелета (Task 0017):
приём вебхука Telegram Bot API, нормализация в общий контракт канала, идемпотентная
запись диалога, вызов AI-оркестратора, уведомления службе о заявках и подтверждения
гостю. Канал — композиционный слой (§5.1), **не порт ядра** (§8): обязательного
Fake-адаптера нет, в тестах он воспроизводится payload'ами вебхука.

> **Сквозная сборка (Task 0017, ADR-011).** На текст гостя канал зовёт оркестратор
> (`guest.py`) и хранит между ходами историю диалога и `pending_action` (гейт P-9).
> Создав заявку, публикует событие `request.created` (через `modules/requests`) —
> уведомление службе и подтверждение гостю идут **подписчиками событий** (P-6,
> `notifications.py`), а не прямыми вызовами. Персонал закрывает заявку командами в
> staff-чате (`staff.py`). Обратная адресация «заявка → чат гостя» — таблица
> `request_origins` (композиционный слой владеет ею, домен не тронут, ADR-011).

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
   второй эффект (общая опора и для гостя, и для команд персонала).
5. **Развилка гость / персонал** (`service.py`): чат == `TELEGRAM_STAFF_CHAT_ID` →
   команда персонала (`staff.py`); иначе — реплика гостю.
   - **Гость** (`guest.py`): не-текст → вежливый отказ; текст → оркестратор
     (история + `pending_action`), ответ гостю. Заявка создана → привязка
     `request_origins`. Ошибка провайдера LLM → деградация §7.8 (честный фолбэк).
   - **Персонал** (`staff.py`): бот реагирует **только на команды** (текст с
     ведущим «/») — `/assign · /start · /done · /cancel <#N>` →
     `requests.change_request_status`, ответ персоналу. Аргумент — **дневной
     номер `#N`** из уведомления (резолв незакрытой заявки среди открытых;
     неоднозначность номера, повторившегося за сутки, — просьба уточнить полным
     id; полный UUID тоже принимается). Обычная переписка группы и не-текст
     остаются без ответа (иначе бот спамит живую группу — её мьютят вместе
     с уведомлениями, #38 п.4).
6. **Уведомления** (`notifications.py`, подписчики событий, P-6): `request.created`
   → в staff-чат (**номер `#N`** в шапке, **номер комнаты**, категория, суть,
   команды `/done N …` — номер и комнату подписчик дочитывает из заявки, событие
   их не несёт, #37/#38); `request.status_changed(done)` → подтверждение гостю
   (адрес — по `request_origins`). Регистрируются composition root воркера
   (`hospitality/worker.py`).

## Файлы

| Файл | Что даёт |
| --- | --- |
| `../base.py` | `NormalizedMessage`, `MessageKind`, `ReplyTo` — контракт всех каналов (P-7) |
| `schemas.py` | Подмножество payload Telegram Bot API (`TelegramUpdate`, …), `extra="ignore"` |
| `normalize.py` | `TelegramUpdate` → `NormalizedMessage` (чистая функция) |
| `models.py` | `Conversation`, `Message`, `RequestOrigin` — тенантные таблицы (канон RLS Task 0009) |
| `store.py` | Идемпотентная запись диалога, состояние гейта P-9, привязки заявок (P-8) |
| `client.py` | `TelegramSender` (порт отправки) + боевая реализация на httpx |
| `outbound.py` | `send_reply` — best-effort отправка + запись исходящего (гость/персонал) |
| `service.py` | `process_update` — приём, маппинг чата на тенанта, развилка гость/персонал |
| `guest.py` | Гостевой ход: вызов оркестратора, история, `pending_action`, привязка заявки |
| `staff.py` | Команды персонала `/assign · /start · /done · /cancel <id>` (Task 0017) |
| `notifications.py` | Подписчики событий: уведомление службе, подтверждение гостю (P-6) |
| `router.py` | Вебхук `POST /channels/telegram/webhook` + проверка секрета (§8.4) |

## Таблицы (миграции `0008`, `0009`, RLS — копия канона `0002`)

- `conversations` — `id`, `tenant_id` (FK+индекс), `channel`, `external_id`
  (уникальны в тройке с `tenant_id`, `channel`), `pending_action` (JSONB, NULL —
  состояние гейта P-9 между ходами, Task 0017), `created_at`, `updated_at`.
- `messages` — `id`, `tenant_id` (FK+индекс), `conversation_id` (FK+индекс),
  `direction` (inbound/outbound), `content_kind` (text/unsupported), `text`,
  `external_message_id`, `idempotency_key` (уникален в паре с `tenant_id`;
  NULL у исходящих реплик; у уведомлений — ключ дедупа `staff:…`/`guest:…`),
  `correlation_id`, `created_at`.
- `request_origins` (Task 0017, ADR-011) — `id`, `tenant_id` (FK+индекс),
  `request_id` (уникален в паре с `tenant_id`, **без FK** на `service_requests`),
  `conversation_id` (FK+индекс), `created_at`. Привязка «заявка → диалог-источник»
  для доставки подтверждения гостю; в Phase 1 вытесняется идентичностью гостя.

Все три таблицы под RLS (ENABLE + FORCE + политика `tenant_isolation`); изоляция
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
- `TELEGRAM_STAFF_CHAT_ID` (Task 0017) — chat.id staff-чата: уведомления о заявках +
  команды закрытия; пустой = служба-канал выключена. **Нужен и воркеру** (уведомления
  шлёт он) — см. `ops/deploy/docker-compose.staging.yml`.

Регистрация вебхука и секретов на staging — `docs/runbooks/telegram.md`.

## События (Task 0017, P-6)

Публикует: ничего напрямую — заявки и их события создаёт `modules/requests` через
оркестратор/инструмент. Потребляет (подписчиками `notifications.py`, регистрирует
`hospitality/worker.py`): `request.created` → уведомление службе;
`request.status_changed` → подтверждение гостю при `done`.

## Зависимости

Внутренние: kernel — `hospitality.shared` (db, tenancy, errors, logging, config,
middleware, events) и `hospitality.platform.models`; доменный `modules/requests`
(через `api.py`: сервис + события) и композиционный `ai` (оркестратор + gateway) —
канал выше них по слоям (§5.1). Внешние: `httpx` (Bot API). Импортируется
composition root'ами: `hospitality/app.py` (`router`), `hospitality/worker.py`
(`notifications`), `alembic/env.py` (`models`).

## Типовые сценарии изменения

- **Новый тип входящего (фото/голос с разбором)** — тип в `MessageKind` +
  обработка в `normalize.py`/`guest.py` + тесты; таблицы не трогаются.
- **Новая staff-команда** — глагол → статус в `_STATUS_BY_VERB` (`staff.py`) + тест.
- **Другой канал уведомлений (не только гостю в Telegram)** — вынести подписчиков в
  общий `notifications/` над портом отправки (ADR-011, «Последствия»).
- **Идентичность гостя (`guests/`, Phase 1)** — заменить `request_origins` резолвом
  идентичности; ADR-011 → superseded.
- **Несколько отелей за одним ботом** — заменить маппинг по slug на пер-чатовую
  таблицу привязки (`_resolve_tenant` в `service.py`); Phase 1.
- **Новый канал (WhatsApp/Email)** — новый пакет `channels/<name>` по этому образцу;
  контракт `NormalizedMessage` не меняется, WhatsApp лишь реализует сборку
  `reply_to.text` по `external_message_id` из истории.
