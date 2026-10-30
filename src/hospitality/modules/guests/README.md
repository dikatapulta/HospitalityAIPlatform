# guests — гости, проживания, привязка гостя к Stay

## Назначение

Доменный модуль гостевой идентичности (FOUNDATION §5.1: «гости, проживания,
профили»; ADR-008 §3–§4, spec 0027, spec 0033 §6): `Guest` и его
идентификаторы в каналах, `Stay` — источник истины про доступ гостя, per-stay
код заселения, ссылка привязки (QR листка гостю) и гостевые сессии. Модуль не знает о
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
(tenant, room), а основной путь привязки — QR листка вовсе без ввода.

## Состав

| Файл | Что даёт |
| --- | --- |
| `models.py` | `Guest`, `GuestIdentity`, `Stay`, `StayAccessCode`, `StayBindLink`, `GuestSession` (все — RLS-канон 0002) |
| `service.py` | Заселение/выезд/перевыпуск кода (гасят и ссылки привязки); переселение/продление; привязка тройкой тенант+комната+код; общий путь рождения идентичности и сессии; резолв сессии; счётчик привязок |
| `expiry.py` | Просроченные Stay (spec 0043): автовыезд по запасу тенанта шагом воркера (тем же `close_stay`, что ручной выезд, строка `FOR UPDATE SKIP LOCKED`) и выборка «Не выехали в срок» для кабинета |
| `bindlink.py` | Ссылка привязки с листка: выпуск (в БД — SHA-256 токена) и привязка по ней тем же путём, что код; срок — от Stay, многоразова |
| `schemas.py` | Pydantic-границы: `StayCheckIn(Result)`, `GuestSessionStart/Bind/Grant`, `ActiveGuestSession`, `StayRead` |
| `events.py` | `stay.checked_in` / `stay.checked_out` (подписчиков в серии 0027 нет) |
| `api.py` | Публичный интерфейс (единственная точка входа извне) |

## Публичный API

- `check_in(StayCheckIn) -> StayCheckInResult` — Guest + Stay(`checked_in`,
  `guests_count`) + код одной транзакцией; plaintext кода возвращается РОВНО
  ОДИН РАЗ. Комната с активным Stay — `ERR-GUESTS-002` (409).
- `reissue_access_code(stay_id) -> str` — новый код гасит старый и все ссылки
  привязки Stay (потерянный листок умирает целиком); привязки и сессии живут.
  `check_out(stay_id) -> StayRead` — выезд: код, ссылки и сессии гаснут.
  `move_stay(stay_id, room) -> StayRead` — переселение (занятая комната —
  `ERR-GUESTS-002`); `extend_stay(stay_id, check_out_at) -> StayRead` — правка
  срока (прошлое — `ERR-GUESTS-004`, 422); доступ гостя в обоих случаях
  следует за Stay сам. Все — `ERR-GUESTS-001` (404), если активного Stay нет.
  Все пять (заселение, перевыпуск, выезд, переселение, продление) принимают
  kw-only `acting_user: StaffActor | None` — сотрудник кабинета: строка
  журнала изменений той же транзакцией (spec 0041 §2.2; снапшот — комната,
  у переселения откуда и куда, имени гостя нет). `extend_stay` принимает и
  `nights` — число ночей для журнала явно, а не разницей сроков: у
  просроченного Stay новый срок считается от сегодня. `None` (CLI) — журнал
  не пишется.
- `issue_bind_link(stay_id) -> str` — ссылка привязки для QR листка (spec
  0033 §6, #354): plaintext токена — РОВНО ОДИН РАЗ, в БД SHA-256. Своего
  срока нет — действует, пока Stay в `checked_in` и `now < check_out_at`;
  повторный выпуск прежние ссылки не гасит. Нет активного Stay —
  `ERR-GUESTS-001`. Rate-limit'ы — забота вызывающих (кабинет/канал).
- `start_guest_session(GuestSessionStart) -> GuestSessionGrant | None` —
  привязка по коду: активный Stay комнаты → bcrypt-verify → новая
  `GuestIdentity` + `GuestSession`. Любой невалидный исход — `None` без
  уточнения причины (перечисление комнат запрещено). Rate-limit ввода — забота
  канала (spec 0027 §3.3).
- `start_guest_session_by_bind_link(GuestSessionBind) -> GuestSessionGrant |
  None` — привязка по токену с листка (многоразово): ссылка, её Stay и срок
  проверяются одним запросом с рождением сессии; дальше ТОТ ЖЕ путь, что у
  кода (P-12). Погашена, чужой тенант, Stay погас — `None`.
- `resolve_session(token) -> ActiveGuestSession | None` — валидность на каждом
  действии; `None` — канал отвечает статическим auth-only ответом.
- `find_active_stay(room)` / `get_active_stay(stay_id)` / `list_active_stays()`
  — взгляд ПЕРСОНАЛА: `checked_in` без фильтра по сроку (просроченный Stay
  занимает комнату и обязан быть видимым); гостевая привязка, наоборот, срок
  проверяет. `list_overdue_stays()` — `checked_in` со сроком в прошлом,
  давние сверху, до 100 (список кабинета, spec 0043 §2.5).
  `count_stay_sessions(stay_id)` — живые привязки (индикатор
  «гость подключился»). `format_access_code(code)`, `WEB_IDENTITY_KIND` — для
  кабинета, CLI и канала web. Гонка перевыпусков — `ERR-GUESTS-003` (409).

- `close_overdue_stays() -> int` — один прогон автовыезда по всем
  настроенным тенантам (зовёт воркер, spec 0043 §2.2): Stay, у которого с
  `check_out_at` прошло `TenantConfig.stay_auto_checkout_grace_hours`
  (умолчание 2, `null` — выключено), закрывается тем же путём, что
  `check_out` (код, ссылки, сессии гаснут, `stay.checked_out`), строка журнала
  «Выезд по сроку» — без личности. Гостю ничего не меняет: доступ погас ещё в
  `check_out_at`. Сбой прогона — `ERR-GUESTS-007`. Выезд, продление и переселение
  берут строку `FOR UPDATE`: «Продлить» в секунду автовыезда либо успевает,
  либо получает `ERR-GUESTS-001`.

## События

- Публикует: `stay.checked_in`, `stay.checked_out` (payload: `stay_id`,
  `room_number`; кода в payload нет никогда). Потребляет: ничего.

## Таблицы

Миграции `0014` + `0019` + `0027` (все — тенантные, RLS-канон 0002; PII —
см. `docs/PII_REGISTRY.md`):

- `guests` — человек; `display_name` (PII, nullable).
- `guest_identities` — идентификатор в канале; UNIQUE
  `(tenant_id, kind, external_id)`; kinds: `telegram|web|phone|email|pms`
  (создаётся пока только `web`).
- `stays` — проживание; partial UNIQUE «один активный Stay на комнату»
  (`uq_stays_tenant_room_checked_in`); `guests_count` (0019; кнопки «1–4» и
  поле «другое» кабинета, `--guests` CLI; потолок `MAX_GUESTS_PER_STAY` в
  `api.py`; вход квот #124).
- `stay_access_codes` — bcrypt-хэш кода; partial UNIQUE «один активный код
  на Stay».
- `stay_bind_links` — ссылки привязки (0027): SHA-256-хэш токена, UNIQUE
  `(tenant_id, token_hash)`, `revoked_at`; своего срока нет — его даёт Stay.
  Активных ссылок у Stay может быть несколько.
- `guest_sessions` — SHA-256-хэш токена, `consent_at`/`consent_version`
  (юраудит 22.07), `last_used_at` (обновляется лениво, раз в ~5 минут).

## Зависимости

Внутренние: `hospitality.shared` (Base/UTCDateTime/utc_now, session_scope,
DomainEvent/publish, AppError, логирование, метрики), `hospitality.platform.staff_actions`
(журнал изменений кабинета, spec 0041), `hospitality.platform.config` (запас
автовыезда и список тенантов для прогона, spec 0043). Внешние сверх общих: `bcrypt`
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
