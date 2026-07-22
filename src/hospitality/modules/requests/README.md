# requests — единый конвейер заявок (CANONICAL MODULE)

> **CANONICAL** (Task 0012, R-10): эталонный доменный модуль. Новый модуль
> создаётся копированием его анатомии и паттернов, отклонение — только через
> обсуждение/ADR.

Назначение: заявки служб отеля — housekeeping, инженерия, IT, F&B, жалобы —
как один механизм с одним жизненным циклом и разными **категориями**
(FOUNDATION §5.2). Новый тип заявки = строка `RequestCategory` у тенанта,
а не новый модуль и не кастомный код.

## Анатомия (§5.2 — копируется каждым новым модулем)

| Файл | Что даёт |
| --- | --- |
| `api.py` | Публичный интерфейс: единственная точка импорта извне (R-5) |
| `models.py` | `RequestCategory`, `ServiceRequest` — тенантные таблицы (канон RLS Task 0009); `RequestStatus` — жизненный цикл |
| `service.py` | `create_category`, `create_request`, `change_request_status`, `get_request`, `list_requests`, `list_categories`, `find_open_requests_by_daily_number`; карта переходов `STATUS_TRANSITIONS`; присвоение дневного номера; коды ошибок |
| `events.py` | `RequestCreated`, `RequestStatusChanged` (канон событий Task 0010) |
| `schemas.py` | Pydantic-схемы границ: `*Create` на входе, `*Read` на выходе (R-6); страница списка `ServiceRequestPage` |
| `router.py` | **CANONICAL ENDPOINT** (Task 0013): HTTP API `/api/v1/requests` поверх `service.py` |
| `tests/` | Жизненный цикл, публикация событий, изоляция тенантов, HTTP API |

## Публичный API (`api.py`)

- `create_request(ServiceRequestCreate) -> ServiceRequestRead` — заявка в
  статусе `new` + событие `request.created` в той же транзакции. Присваивает
  **дневной номер `#N`** (см. ниже). Принимает необязательный `guest_language`
  (ISO 639-1) — язык гостя для статусных уведомлений (spec 0021 П-1).
- `change_request_status(request_id, RequestStatus, resolution_note=) ->
  ServiceRequestRead` — переход по жизненному циклу + событие
  `request.status_changed`. `resolution_note` — примечание персонала к закрытию
  (частичное выполнение / причина отмены, spec 0021 П-4): пишется только на
  терминальном переходе, на прочих игнорируется с warning-логом.
- `get_request(request_id) -> ServiceRequestRead`.
- `find_open_requests_by_daily_number(daily_number) -> list[ServiceRequestRead]`
  — незакрытые заявки тенанта с этим дневным номером (резолв команды `/done N`
  в staff-чате). Список, а не одна: номер за сутки может повториться, тогда
  вызывающая сторона просит уточнить (см. «Дневной номер»).
- `list_requests(limit=, offset=) -> ServiceRequestPage` — страница заявок
  тенанта, новые сверху (канон пагинации Task 0013).
- `list_categories() -> list[RequestCategoryRead]` — категории тенанта по `key`.
- `create_category(RequestCategoryCreate) -> RequestCategoryRead` — в Phase 0
  вызывается сидами и тестами.
- `router` — HTTP-роутер (ниже); подключает только composition root.

Все функции вызываются внутри `tenant_context(...)` (P-4) и сами управляют
транзакцией (`session_scope()` внутри). Ожидаемые ошибки — `AppError`
с кодами `ERR-REQUESTS-001…004` (каталог: `docs/runbooks/errors.md`).

## HTTP API (`router.py`, CANONICAL ENDPOINT — Task 0013)

Эталон REST-эндпоинта платформы (§11, §13.5, P-7): версия `/v1/` в пути,
аутентификация сервисным токеном (`Authorization: Bearer <SERVICE_TOKEN>`,
без токена — 401 `ERR-PLATFORM-007`), схемы модуля на границах, ошибки —
канонический конверт с кодами каталога, пагинация `limit`/`offset` + `total`.
Тенанта устанавливает `TenantContextMiddleware` по токену — API его не
принимает и не возвращает.

| Метод и путь | Что делает | Ошибки |
| --- | --- | --- |
| `POST /api/v1/requests` | Создать заявку (201) | 404 `ERR-REQUESTS-001` |
| `GET /api/v1/requests?limit=&offset=` | Список заявок, новые сверху | — |
| `GET /api/v1/requests/categories` | Категории тенанта | — |
| `GET /api/v1/requests/{id}` | Заявка по id | 404 `ERR-REQUESTS-002` |
| `POST /api/v1/requests/{id}/status` | Переход по жизненному циклу | 404 `ERR-REQUESTS-002`, 409 `ERR-REQUESTS-003` |

## Жизненный цикл статусов (ADR-013)

```
new → in_progress → done
  └────────┴─→ cancelled        (done, cancelled — терминальные)
```

Статуса `assigned` больше нет (ADR-013, issue #75): персонал пилота не различал
«назначено» и «в работе». «Кто взял» появится в Phase 1 атрибутом assignee.

Недопустимый переход (в т.ч. в тот же статус) — `ERR-REQUESTS-003` (409).
Карта переходов — `STATUS_TRANSITIONS` в `service.py`.

## События

- Публикует: `request.created` (`RequestCreated`: request_id, category_id,
  summary), `request.status_changed` (`RequestStatusChanged`: request_id,
  old_status, new_status). Публикация — атомарно с бизнес-записью (P-6,
  outbox ADR-005).
- Потребляет: ничего. Подписчики (уведомление службы и подтверждение гостю —
  `channels/telegram/notifications.py`, Task 0017) регистрируются composition
  root'ом воркера (`hospitality/worker.py`), модуль о них не знает (P-6).

## Дневной номер `#N` (issue #38, миграция `0010`)

Заявка получает человеческий номер `#12` — для глаз, речи и отчёта («возьми
12») вместо 36-символьного UUID. Номер уникален в паре `(тенант, день отеля)`
и **сбрасывается раз в сутки** по локальной полуночи отеля (tz из конфига
тенанта, §9; тенант без конфига — деградация на UTC, не отказ). Разные дни
могут повторять `#12`: номер — **метка, не ключ действия**, поэтому резолв по
номеру (`find_open_requests_by_daily_number`) возвращает список, а
неоднозначность разрешает человек.

- **День** заявки хранится в колонке `service_day` (локальная дата), номер — в
  `daily_number`; присвоение — `max(daily_number)+1` за этот день.
- **Защита от гонки** — сам уникальный индекс: параллельный создатель, занявший
  тот же номер, ловит `IntegrityError`, `create_request` пересчитывает номер и
  повторяет (номер не дублируется и не «дырявится»).

## Таблицы (миграции `0006`, `0010`, `0012`, `0013`; RLS — копия канона `0002`)

- `request_categories` — `id`, `tenant_id` (FK+индекс), `key`
  (уникален в паре с `tenant_id`), `name`, `created_at`, `updated_at`.
- `service_requests` — `id`, `tenant_id` (FK+индекс), `category_id`
  (FK+индекс), `status` (VARCHAR, значения `RequestStatus`), `summary`,
  `details`, `room_number`, `service_day` (DATE, NULL), `daily_number`
  (INT, NULL), `guest_language` (VARCHAR(2), NULL — ISO 639-1 язык гостя на
  момент создания, для статусных уведомлений, spec 0021 / миграция `0012`),
  `resolution_note` (VARCHAR(500), NULL — примечание персонала к закрытию,
  spec 0021 / миграция `0013`), `created_at`, `updated_at`. Тройка
  `(tenant_id, service_day, daily_number)` — уникальный индекс
  `uq_service_requests_daily_number` (дневной номер, миграция `0010`).

Обе таблицы под RLS (ENABLE + FORCE + политика `tenant_isolation`);
изоляция покрыта обязательными тестами (`tests/test_tenant_isolation.py`
модуля).

## Зависимости

Внутренние: kernel — `hospitality.shared` (db, tenancy, events, errors,
logging), `hospitality.platform.auth` (аутентификация роутера, Task 0013) и
`hospitality.platform.config` (`load_tenant_config` — часовой пояс отеля для
дневного номера).
Других доменных модулей не импортирует; сам импортируется только через
`api.py` (контракт import-linter «module internals are private»).

## Типовые сценарии изменения

- **Новая категория заявок у отеля** — не код: строка в `request_categories`
  (сид/онбординг). Маршрутизация в службу и SLA категории — Phase 1,
  добавлением колонок.
- **Новое поле заявки** — колонка в `ServiceRequest` + миграция + поле в
  схемах + README; RLS-блок не трогается.
- **Новый потребитель событий** — подписчик в своём модуле/слое +
  регистрация в `hospitality/worker.py`; этот модуль не меняется.
- **Новый статус/переход** — значение в `RequestStatus`, ребро в
  `STATUS_TRANSITIONS`, миграция данных при необходимости, тесты переходов.
