# channels/web — гостевой веб-чат по QR (spec 0027, issue #79)

## Назначение

Второй гостевой канал: гость сканирует статический QR в номере
(`https://<хост>/g/{tenant_slug}/{room}`), вводит код заселения с карточки
ресепшена (ADR-008 §3) и общается с тем же AI-консьержем, что в Telegram.
**Строгий auth-only с рождения** (Q7, решение 22.07): без валидной
`GuestSession` — статический ответ `ERR-WEB-002` без единого вызова LLM и без
заявок; после выезда — то же (Q8, валидность перепроверяется на каждом
действии). Транспорт — HTTP JSON + одна статическая страница; ход гостя —
общий `channels/common/guest_turn` (spec 0027 §2).

## Маршруты (`/g/{tenant_slug}/{room_number}`)

| Маршрут | Auth | Что делает |
| --- | --- | --- |
| `GET …/` | нет | HTML-страница (двуязычная en+ru, инлайн CSS/JS, `page.py`) |
| `POST …/session` | нет, rate-limit | `{code}` → тройка тенант+комната+код → Set-Cookie `guest_session`; отказ — 403 `ERR-WEB-003` без причины; лимит по (tenant, room) — 429 `ERR-WEB-004` |
| `POST …/messages` | сессия | `{text, client_message_id}` → общий ход гостя → `{replies}` синхронно; повтор `client_message_id` — `duplicate: true` (P-8) |
| `GET …/messages?after=` | сессия | история/новые сообщения (poll раз в ~5 с; так доезжают подтверждения заявок от подписчика) |

Контекст тенанта канал ставит САМ по slug внутри маршрутов (ADR-008 §6):
ни QR-slug, ни гостевая сессия НЕ входят в общий `TenantResolver` — гостевая
сессия на `/api/v1/*` конструктивно даёт 401 (тест-инвариант). Cookie:
HttpOnly + Secure + SameSite=Strict + Path=/g/{slug}; атрибуты — не граница
безопасности, границу держит `guests.resolve_session` на каждом действии.
`{room}` из пути на аутентифицированных операциях игнорируется — комната
только из сессии (несовпадение логируется `web_path_room_mismatch`).

## Ключевые решения

- **Комната — из привязки, не из текста** (issue #79): `verified_room_number`
  Stay уезжает в ход (`ToolTurnContext`) — `create_service_request`
  перезаписывает комнату из аргументов модели; системный промпт получает блок
  «verified room» (модель не переспрашивает номер). Эскалации web-гостя несут
  комнату — персоналу есть куда прийти.
- **Ключ чат-лимита — `stay_id`** (spec 0027 §3.2): повторный ввод кода рождает
  новую идентичность и не должен обнулять лимиты spec 0023.
- **Доставка исходящих — только запись** в `messages` (poll забирает);
  канал-осознанный подписчик — `channels/telegram/notifications.py`.
- Согласие на обработку ПД фиксируется при привязке
  (`service.CONSENT_VERSION` → `guest_sessions.consent_*`); тексты — черновик
  v0, финальные kk/ru/en — юрпакет.

## Файлы

| Файл | Что даёт |
| --- | --- |
| `router.py` | Маршруты, cookie, зависимость `_require_session` (auth-only 401) |
| `service.py` | Резолв тенанта по slug, привязка (rate-limit кода), ход, poll |
| `schemas.py` | Pydantic-границы HTTP (R-6) |
| `page.py` | Статическая страница (инлайн CSS/JS; Next.js — только кабинет, ADR-002) |

## Таблицы

Своих нет: диалог — общие `conversations`/`messages` (`channel='web'`,
`external_id` = UUID идентичности клиента; `conversations.guest_identity_id`
заполняется при создании — миграция 0015); доступ — таблицы `modules/guests`.

## Конфигурация

- `GUEST_CODE_VERIFY_RATE_LIMIT_ATTEMPTS` / `…_WINDOW_SECONDS` — лимит ввода
  кода по (tenant, room); ≤0 отключает. Чат-лимиты — общие
  `GUEST_CHAT_RATE_LIMIT_*` (spec 0023).
- `TenantConfig.reception_phone` — телефон в статическом auth-only ответе.

## Наблюдаемость

Логи: `guest_web_session_started`(в modules/guests: `guest_session_started`) /
`guest_code_rejected` / `guest_web_unauthenticated` /
`guest_web_code_rate_limited` / `web_path_room_mismatch`. Метрики:
`guest_web_sessions_total{outcome}`, `guest_rate_limited_total{scope}`.
Коды: ERR-WEB-001…004 (docs/runbooks/errors.md).

## Типовые сценарии изменения

- **Публичные вопросы неавторизованным** (Phase 2, Q7) — per-channel конфиг
  тенанта; auth-only здесь остаётся дефолтом.
- **WebSocket/SSE вместо poll** — деталь транспорта; API и ход не меняются.
- **Финальные тексты согласия (юрпакет)** — `page.py` + `CONSENT_VERSION`.
- **Auth-only в Telegram** — тот же `guests.start_guest_session` с
  `identity_kind=telegram`; этот канал не меняется.
