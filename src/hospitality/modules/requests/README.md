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
| `service.py` | `create_category`, `create_request`, `change_request_status`, `get_request`; карта переходов `STATUS_TRANSITIONS`; коды ошибок |
| `events.py` | `RequestCreated`, `RequestStatusChanged` (канон событий Task 0010) |
| `schemas.py` | Pydantic-схемы границ: `*Create` на входе, `*Read` на выходе (R-6) |
| `tests/` | Жизненный цикл, публикация событий, изоляция тенантов |

## Публичный API (`api.py`)

- `create_request(ServiceRequestCreate) -> ServiceRequestRead` — заявка в
  статусе `new` + событие `request.created` в той же транзакции.
- `change_request_status(request_id, RequestStatus) -> ServiceRequestRead` —
  переход по жизненному циклу + событие `request.status_changed`.
- `get_request(request_id) -> ServiceRequestRead`.
- `create_category(RequestCategoryCreate) -> RequestCategoryRead` — в Phase 0
  вызывается сидами и тестами.

Все функции вызываются внутри `tenant_context(...)` (P-4) и сами управляют
транзакцией (`session_scope()` внутри). Ожидаемые ошибки — `AppError`
с кодами `ERR-REQUESTS-001…004` (каталог: `docs/runbooks/errors.md`).

## Жизненный цикл статусов

```
new → assigned → in_progress → done
  └──────┴────────────┴─→ cancelled        (done, cancelled — терминальные)
```

Недопустимый переход (в т.ч. в тот же статус) — `ERR-REQUESTS-003` (409).
Карта переходов — `STATUS_TRANSITIONS` в `service.py`.

## События

- Публикует: `request.created` (`RequestCreated`: request_id, category_id,
  summary), `request.status_changed` (`RequestStatusChanged`: request_id,
  old_status, new_status). Публикация — атомарно с бизнес-записью (P-6,
  outbox ADR-005).
- Потребляет: ничего. Подписчики (уведомление службы — Task 0017)
  регистрируются composition root'ом воркера, модуль о них не знает.

## Таблицы (миграция `0005`, RLS — копия канона `0002`)

- `request_categories` — `id`, `tenant_id` (FK+индекс), `key`
  (уникален в паре с `tenant_id`), `name`, `created_at`, `updated_at`.
- `service_requests` — `id`, `tenant_id` (FK+индекс), `category_id`
  (FK+индекс), `status` (VARCHAR, значения `RequestStatus`), `summary`,
  `details`, `room_number`, `created_at`, `updated_at`.

Обе таблицы под RLS (ENABLE + FORCE + политика `tenant_isolation`);
изоляция покрыта обязательными тестами (`tests/test_tenant_isolation.py`
модуля).

## Зависимости

Внутренние: только kernel — `hospitality.shared` (db, tenancy, events,
errors, logging). Других доменных модулей не импортирует; сам импортируется
только через `api.py` (контракт import-linter «module internals are
private»).

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
