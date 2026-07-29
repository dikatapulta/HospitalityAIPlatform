# channels/common — общее ядро гостевых каналов (spec 0027 §2)

## Назначение

Канал-агностичная часть гостевых каналов: единая персистенция диалога,
общий «ход гостя» и событие эскалации. Транспортные адаптеры
(`channels/telegram`, `channels/web`, будущий WhatsApp) зовут это ядро —
инварианты spec 0022/0023/0025 живут в одном месте, а не в копиях. Вынесено
из `channels/telegram` с приходом второго канала (PR #113, поведение не
менялось). Композиционный слой (§5.1); ядро НЕ импортирует транспорты.

## Состав

| Файл | Что даёт |
| --- | --- |
| `models.py` | `Conversation`, `Message`, `RequestOrigin` — тенантные таблицы диалога (§9, RLS-канон; миграции 0008/0009/0015) |
| `store.py` | Идемпотентная запись диалога (P-8), гейт P-9, привязки заявок, окно истории `MAX_HISTORY_MESSAGES` (#74), выборка для страницы веб-чата, обратный поиск заявки по реплаю на сообщение бота (белый список ключей `staff:request_created`/`staff:request_unclaimed`/`staff:note_prompt`) |
| `guest_turn.py` | `run_guest_turn` — ход гостя: rate-limit 0023 ДО оркестратора → история/pending/снапшот 0025 → оркестратор → эскалация 0022 (в outbox ДО реплики) → привязка ADR-011; транспорт — параметр `reply`, ключ лимита — параметр `rate_limit_key` (telegram — chat_id, web — stay_id), `verified_room_number` — комната из привязки (web) |
| `consent.py` | **CANONICAL** согласие гостя (spec 0029): `CONSENT_VERSION`, тексты kk/ru/en (дословная копия `docs/legal/consent-text.md`, дрейф ловит `tests/test_legal.py`), выбор языка и правило `is_consent_current`. Где ХРАНИТСЯ факт — забота канала: telegram — `conversations.consent_at/_version`, web — `guest_sessions` (§1 спеки) |
| `events.py` | `ConversationEscalated` + `publish_escalation` (канон `platform/events.py`) |

Статические тексты (UNSUPPORTED/DEGRADED/rate-limit) и лог-код
`ERR-CHANNEL-003` — здесь же: они двуязычны и канал-нейтральны.
`refuse_if_rate_limited` публична: consent-gate обязан считать те же ступени,
что и ход гостя (одно место правила, P-12).

## События

Публикует: `conversation.escalated` (spec 0022). Потребляет: ничего
(подписчики уведомлений — `channels/telegram/notifications.py`, staff живёт в
Telegram; гостевые уведомления там канал-осознанны: telegram — push, web —
запись исходящего под poll).

## Таблицы

`conversations` / `messages` / `request_origins` — описание и колонки в
`channels/telegram/README.md` (исторически) и в докстрингах `models.py`;
`conversations.guest_identity_id` (0015) — аддитивная связь с
`modules/guests`, без FK (граница модулей);
`conversations.consent_at`/`consent_version` (0016) — доказательная запись
согласия telegram-гостя (spec 0029, строка в `docs/PII_REGISTRY.md`).

## Зависимости

Kernel (`shared`), домен `modules/requests` (api), композиционный `ai`
(оркестратор/gateway/tools) и `channels/base`. Транспорты (`telegram`, `web`)
импортируют ядро; обратное запрещено (контракт import-linter «common не
импортирует транспорты»).

## Типовые сценарии изменения

- **Новый гостевой канал** — транспортный пакет `channels/<name>`, зовущий
  `ensure_conversation`/`insert_inbound_message`/`run_guest_turn` со своим
  `reply`; ядро не меняется (карта канонов CLAUDE.md).
- **Новый инвариант хода** (лимит, политика, квота) — здесь, один раз; каналы
  получают его бесплатно.
- **Правка текстов отказов** — `guest_turn.py` (двуязычные, без LLM).
- **Правка текста согласия** — сначала `docs/legal/consent-text.md` (источник
  истины), затем копия в `consent.py` и `CONSENT_VERSION` — в одном PR: гости с
  прежней версией пройдут гейт заново (spec 0029 §6).
