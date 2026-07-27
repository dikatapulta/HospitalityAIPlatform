# guests — гости, проживания, привязка гостя к Stay

## Назначение

Доменный модуль гостевой идентичности (FOUNDATION §5.1: «гости, проживания,
профили»; ADR-008 §3–§4, spec 0027): `Guest` и его идентификаторы в каналах,
`Stay` — источник истины про доступ гостя, per-stay код заселения и гостевые
сессии. Модуль не знает о каналах и AI (R-5): проверку кода и выдачу сессии
зовут каналы (`channels/web`, позже auth-only Telegram), заселение — CLI
(`tools/checkin`, до кабинета #48).

Инварианты (ADR-008): секреты в БД — только хэши (код — bcrypt, токен —
SHA-256); срок жизни кода и сессий производен от Stay — продление/ранний выезд
правят Stay, доступ следует автоматически; валидность сессии перепроверяется
на каждом действии («истёкшая сессия не может действовать», DoD #79); после
выезда доступ гаснет без grace-периода (Q8).

## Состав

| Файл | Что даёт |
| --- | --- |
| `models.py` | `Guest`, `GuestIdentity`, `Stay`, `StayAccessCode`, `GuestSession` (все — RLS-канон 0002) |
| `service.py` | Заселение/выезд/перевыпуск кода; привязка тройкой тенант+комната+код; резолв сессии |
| `schemas.py` | Pydantic-границы: `StayCheckIn(Result)`, `GuestSessionStart/Grant`, `ActiveGuestSession`, `StayRead` |
| `events.py` | `stay.checked_in` / `stay.checked_out` (подписчиков в серии 0027 нет) |
| `api.py` | Публичный интерфейс (единственная точка входа извне) |

## Публичный API

- `check_in(StayCheckIn) -> StayCheckInResult` — Guest + Stay(`checked_in`) +
  код одной транзакцией; plaintext кода возвращается РОВНО ОДИН РАЗ.
  Комната с активным Stay — `ERR-GUESTS-002` (409).
- `reissue_access_code(stay_id) -> str` — новый код гасит старый; привязки и
  сессии живут. `check_out(stay_id) -> StayRead` — выезд: код и сессии гаснут.
  Оба — `ERR-GUESTS-001` (404), если активного Stay нет.
- `start_guest_session(GuestSessionStart) -> GuestSessionGrant | None` —
  привязка канала: активный Stay комнаты → bcrypt-verify кода → новая
  `GuestIdentity` + `GuestSession`. Любой невалидный исход — `None` без
  уточнения причины (перечисление комнат запрещено). Rate-limit ввода — забота
  канала (spec 0027 §3.3).
- `resolve_session(token) -> ActiveGuestSession | None` — валидность на каждом
  действии; `None` — канал отвечает статическим auth-only ответом.
- `find_active_stay(room) -> StayRead | None`, `list_active_stays()` — взгляд
  ПЕРСОНАЛА: `checked_in` без фильтра по сроку (просроченный Stay занимает
  комнату и обязан быть видимым для выезда/перевыпуска); гостевая привязка,
  наоборот, срок проверяет. `format_access_code(code)`, `WEB_IDENTITY_KIND` —
  для CLI и канала web. Гонка перевыпусков — `ERR-GUESTS-003` (409).

## События

- Публикует: `stay.checked_in`, `stay.checked_out` (payload: `stay_id`,
  `room_number`; кода в payload нет никогда). Потребляет: ничего.

## Таблицы

Миграция `0014` (все — тенантные, RLS-канон 0002; первая PII-миграция —
см. `docs/PII_REGISTRY.md`):

- `guests` — человек; `display_name` (PII, nullable).
- `guest_identities` — идентификатор в канале; UNIQUE
  `(tenant_id, kind, external_id)`; kinds: `telegram|web|phone|email|pms`
  (создаётся пока только `web`).
- `stays` — проживание; partial UNIQUE «один активный Stay на комнату»
  (`uq_stays_tenant_room_checked_in`).
- `stay_access_codes` — bcrypt-хэш кода; partial UNIQUE «один активный код
  на Stay».
- `guest_sessions` — SHA-256-хэш токена, `consent_at`/`consent_version`
  (юраудит 22.07), `last_used_at` (обновляется лениво, раз в ~5 минут).

## Зависимости

Внутренние: `hospitality.shared` (Base/UTCDateTime/utc_now, session_scope,
DomainEvent/publish, AppError, логирование). Внешние сверх общих: `bcrypt`
(хэш кода заселения — пространство 30⁶ мало́ для SHA-256, spec 0027 §1.2).

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
