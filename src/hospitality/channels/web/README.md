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

## Маршруты (`/g/{tenant_slug}/{room_number}` и `/w/{tenant_slug}/b/{token}`)

| Маршрут | Auth | Что делает |
| --- | --- | --- |
| `GET /g/…/` | нет | HTML-страница чата (двуязычная en+ru, инлайн CSS/JS, `page.py`) |
| `POST /g/…/session` | нет, rate-limit | `{code}` → тройка тенант+комната+код → Set-Cookie `guest_session`; отказ — 403 `ERR-WEB-003` без причины; лимит по (tenant, room) — 429 `ERR-WEB-004` |
| `POST /g/…/messages` | сессия | `{text, client_message_id}` → общий ход гостя → `{replies}` синхронно; повтор `client_message_id` — `duplicate: true` (P-8) |
| `GET /g/…/messages?after=` | сессия | история/новые сообщения (poll раз в ~5 с; так доезжают подтверждения заявок от подписчика) |
| `GET /w/{slug}/b/{token}` | нет | Страница QR-ссылки привязки (spec 0033 §6): consent-строка v3 + кнопка; токен НЕ потребляется |
| `POST /w/{slug}/b/{token}/session` | нет, rate-limit по IP | Нажатие кнопки-согласия: GETDEL токена → `start_guest_session_for_stay` (тот же путь, что код, P-12) → cookie + `chat_url`; истёк/потреблён — 403 `ERR-GUESTS-006`; лимит — 429 `ERR-WEB-005` |

Короткий префикс `/w` — ради ёмкости QR; отдельный `bind_router` подключает
composition root рядом с основным.

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
- **Экран входа = экран согласия** (spec 0029): кнопка ввода кода и есть
  согласие на обработку ПД. Тексты и версия — общий канон каналов
  (`channels/common/consent.py`, дословная копия `docs/legal/consent-text.md`),
  во всех трёх языках: до входа язык гостя неизвестен всегда. Факт пишется на
  сессию (`guest_sessions.consent_*`) — своё согласие на каждую привязку;
  telegram хранит своё на диалоге, правило актуальности версии общее.
  Страница bind-ссылки — тот же канон: токен потребляется ТОЛЬКО по нажатию
  кнопки-согласия (открытие страницы — не согласие).

## Файлы

| Файл | Что даёт |
| --- | --- |
| `router.py` | Маршруты `/g` + `bind_router` `/w`, cookie (общий `_set_session_cookie` обоих путей), зависимость `_require_session` (auth-only 401) |
| `service.py` | Резолв тенанта по slug, привязка по коду (rate-limit) и по bind-ссылке (rate-limit по IP), ход, poll |
| `schemas.py` | Pydantic-границы HTTP (R-6) |
| `page.py` | Статические страницы чата и bind-ссылки (инлайн CSS/JS; Next.js — только кабинет, ADR-002); текст согласия подставляется из общего канона, ссылка на политику — `/legal/privacy` |

## Таблицы

Своих нет: диалог — общие `conversations`/`messages` (`channel='web'`,
`external_id` = UUID идентичности клиента; `conversations.guest_identity_id`
заполняется при создании — миграция 0015); доступ — таблицы `modules/guests`.

## Конфигурация

- `GUEST_CODE_VERIFY_RATE_LIMIT_ATTEMPTS` / `…_WINDOW_SECONDS` — лимит ввода
  кода по (tenant, room); ≤0 отключает. Чат-лимиты — общие
  `GUEST_CHAT_RATE_LIMIT_*` (spec 0023).
- `GUEST_BIND_LINK_CONSUME_RATE_LIMIT_*` — лимит потребления bind-ссылок по
  IP (spec 0033 §9); просторный — гости за NAT отеля делят один адрес. Сам
  адрес — канон `shared/clientip.py` (issue #207): за туннелем `request.client`
  указывает на соседний контейнер, и лимит был бы общим на всех гостей сразу.
- `TenantConfig.reception_phone` — телефон в статическом auth-only ответе.
- `PUBLIC_BASE_URL` — из него собирается ссылка на политику конфиденциальности
  в тексте согласия на экране входа (spec 0029 §2).

## Наблюдаемость

Логи: `guest_web_session_started`(в modules/guests: `guest_session_started`) /
`guest_code_rejected` / `guest_web_unauthenticated` /
`guest_web_code_rate_limited` / `guest_bind_link_rate_limited` /
`web_path_room_mismatch`; путь bind-ссылки в modules/guests:
`stay_bind_link_issued/consumed/rejected`. Метрики:
`guest_web_sessions_total{outcome: started|rejected|bind_started|bind_rejected}`
(доля QR-ссылки vs ввод кода — метрика spec 0033 §9),
`guest_rate_limited_total{scope}`.
Коды: ERR-WEB-001…005, ERR-GUESTS-006 (docs/runbooks/errors.md).

## Типовые сценарии изменения

- **Публичные вопросы неавторизованным** (Phase 2, Q7) — per-channel конфиг
  тенанта; auth-only здесь остаётся дефолтом.
- **WebSocket/SSE вместо poll** — деталь транспорта; API и ход не меняются.
- **Финальные тексты согласия (юрпакет)** — `page.py` + `CONSENT_VERSION`.
- **Auth-only в Telegram** — тот же `guests.start_guest_session` с
  `identity_kind=telegram`; этот канал не меняется.
