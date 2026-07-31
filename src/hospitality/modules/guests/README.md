# guests — гости, проживания, привязка гостя к Stay

## Назначение

Доменный модуль гостевой идентичности (FOUNDATION §5.1: «гости, проживания,
профили»; ADR-008 §3–§4, spec 0027, spec 0033 §6): `Guest` и его
идентификаторы в каналах, `Stay` — источник истины про доступ гостя, per-stay
код заселения, одноразовая bind-ссылка и гостевые сессии. Модуль не знает о
каналах и AI (R-5): проверку кода/ссылки и выдачу сессии зовут каналы
(`channels/web`, позже auth-only Telegram), заселение и операции над Stay —
кабинет персонала (`staff_portal`, #48) и CLI (`tools/checkin`).

Инварианты (ADR-008): секреты в БД — только хэши (код — bcrypt, токены —
SHA-256); срок жизни кода и сессий производен от Stay — продление/переселение/
ранний выезд правят Stay, доступ следует автоматически; валидность сессии
перепроверяется на каждом действии («истёкшая сессия не может действовать»,
DoD #79); после выезда доступ гаснет без grace-периода (Q8).

Код заселения — **6 цифр** (spec 0033 Ф-2, решение 30.07.2026): надёжнее
рукописно и по телефону. Пространство 10⁶ держат bcrypt + rate-limit по
(tenant, room), а основной путь привязки — QR-ссылка вовсе без ввода.

## Состав

| Файл | Что даёт |
| --- | --- |
| `models.py` | `Guest`, `GuestIdentity`, `Stay`, `StayAccessCode`, `GuestSession` (все — RLS-канон 0002) |
| `service.py` | Заселение/выезд/перевыпуск кода; переселение/продление; привязка тройкой тенант+комната+код и по bind-ссылке (общий путь); резолв сессии; счётчик привязок |
| `bindlink.py` | Одноразовая ссылка привязки: Redis-ключ `bindlink:{tenant}:{sha256(token)}` → stay_id, TTL 120 с, потребление GETDEL; fail-closed |
| `schemas.py` | Pydantic-границы: `StayCheckIn(Result)`, `GuestSessionStart/Bind/Grant`, `ActiveGuestSession`, `StayRead` |
| `events.py` | `stay.checked_in` / `stay.checked_out` (подписчиков в серии 0027 нет) |
| `api.py` | Публичный интерфейс (единственная точка входа извне) |

## Публичный API

- `check_in(StayCheckIn) -> StayCheckInResult` — Guest + Stay(`checked_in`,
  `guests_count`) + код одной транзакцией; plaintext кода возвращается РОВНО
  ОДИН РАЗ. Комната с активным Stay — `ERR-GUESTS-002` (409).
- `reissue_access_code(stay_id) -> str` — новый код гасит старый; привязки и
  сессии живут. `check_out(stay_id) -> StayRead` — выезд: код и сессии гаснут.
  `move_stay(stay_id, room) -> StayRead` — переселение (занятая комната —
  `ERR-GUESTS-002`); `extend_stay(stay_id, check_out_at) -> StayRead` — правка
  срока (прошлое — `ERR-GUESTS-004`, 422); доступ гостя в обоих случаях
  следует за Stay сам. Все — `ERR-GUESTS-001` (404), если активного Stay нет.
- `issue_bind_link(stay_id) -> str` / `consume_bind_link(token) -> stay_id |
  None` — одноразовая QR-ссылка привязки (spec 0033 §6): токен живёт
  `BIND_LINK_TTL_SECONDS` (120 с) в Redis, потребление атомарно (GETDEL).
  Fail-closed: недоступный Redis — `ERR-GUESTS-005` (503) на выпуске и `None`
  на потреблении. Rate-limit'ы — забота вызывающих (кабинет/канал).
- `start_guest_session(GuestSessionStart) -> GuestSessionGrant | None` —
  привязка по коду: активный Stay комнаты → bcrypt-verify → новая
  `GuestIdentity` + `GuestSession`. Любой невалидный исход — `None` без
  уточнения причины (перечисление комнат запрещено). Rate-limit ввода — забота
  канала (spec 0027 §3.3).
- `start_guest_session_for_stay(GuestSessionBind) -> GuestSessionGrant | None`
  — привязка по потреблённой bind-ссылке: ТОТ ЖЕ путь создания идентичности и
  сессии (P-12), без проверки кода — право дала ссылка, выпущенная персоналом.
- `resolve_session(token) -> ActiveGuestSession | None` — валидность на каждом
  действии; `None` — канал отвечает статическим auth-only ответом.
- `find_active_stay(room)` / `get_active_stay(stay_id)` / `list_active_stays()`
  — взгляд ПЕРСОНАЛА: `checked_in` без фильтра по сроку (просроченный Stay
  занимает комнату и обязан быть видимым); гостевая привязка, наоборот, срок
  проверяет. `count_stay_sessions(stay_id)` — живые привязки (индикатор
  «гость подключился»). `format_access_code(code)`, `WEB_IDENTITY_KIND` — для
  кабинета, CLI и канала web. Гонка перевыпусков — `ERR-GUESTS-003` (409).

## События

- Публикует: `stay.checked_in`, `stay.checked_out` (payload: `stay_id`,
  `room_number`; кода в payload нет никогда). Потребляет: ничего.

## Таблицы

Миграции `0014` + `0019` (все — тенантные, RLS-канон 0002; PII —
см. `docs/PII_REGISTRY.md`):

- `guests` — человек; `display_name` (PII, nullable).
- `guest_identities` — идентификатор в канале; UNIQUE
  `(tenant_id, kind, external_id)`; kinds: `telegram|web|phone|email|pms`
  (создаётся пока только `web`).
- `stays` — проживание; partial UNIQUE «один активный Stay на комнату»
  (`uq_stays_tenant_room_checked_in`); `guests_count` (0019, кнопки «1/2/3+»
  кабинета, вход квот #124).
- `stay_access_codes` — bcrypt-хэш кода; partial UNIQUE «один активный код
  на Stay».
- `guest_sessions` — SHA-256-хэш токена, `consent_at`/`consent_version`
  (юраудит 22.07), `last_used_at` (обновляется лениво, раз в ~5 минут).

Bind-ссылки таблицы не имеют: токен живёт в Redis (эфемерность — свойство,
потеря при рестарте лечится перевыпуском одним нажатием).

## Зависимости

Внутренние: `hospitality.shared` (Base/UTCDateTime/utc_now, session_scope,
DomainEvent/publish, AppError, ratelimit.create_redis_client — фабрика
Redis-клиента для bind-ссылок, логирование). Внешние сверх общих: `bcrypt`
(хэш кода заселения — пространство 10⁶ мало́ для SHA-256, spec 0027 §1.2 /
0033 Ф-2).

## Типовые сценарии изменения

- Новый канал привязки (Telegram auth-only) → канал зовёт
  `start_guest_session` со своим `identity_kind`/`external_id`; модуль не
  меняется.
- Слияние дубликатов Guest → перевешивание `guest_id` у `GuestIdentity`
  (модель готова; автоматика — отдельная задача, ADR-008).
- `expected`/`cancelled` статусы Stay (кабинет, PMS) → новые сервисные
  функции; жизненный цикл уже в `StayStatus`.
- Новая PII-колонка → миграция + строка в `docs/PII_REGISTRY.md` в том же PR
  (§9).
