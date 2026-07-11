# platform — тенанты, пользователи, конфигурация (kernel)

<!-- CANONICAL: первый полный паспорт модуля (R-4, Task 0011). Новые модули
копируют СТРУКТУРУ этого файла: назначение → состав → публичный API →
события → таблицы → зависимости → типовые сценарии изменения. -->

## Назначение

Корневой модуль платформы: реестр тенантов и их конфигурация; в следующих
задачах — пользователи, RBAC, аудит, фиче-флаги, лимиты на тенанта
(FOUNDATION §5.1, ADR-003). Слой kernel: доменные модули (`modules/`)
опираются на `platform`, обратное запрещено.

## Состав

| Файл | Что даёт | Задача |
| --- | --- | --- |
| `models.py` | `Tenant` — единица изоляции данных и конфигурации (GLOSSARY); `TenantIsolationCanary` — канонический образец тенантной таблицы | 0008/0009 |
| `events.py` | CANONICAL: `CanaryCreated` + `echo_canary_created` — образец доменного события и идемпотентного подписчика (P-6, P-8) | 0010 |
| `config.py` | CANONICAL: конфигурация тенанта — схема `TenantConfig` со `schema_version` (§6) + `load_tenant_config`/`store_tenant_config` | 0011 |
| `seed.py` | Идемпотентный сид демо-тенанта «Demo Hotel» (`make seed`; выполняется на каждом деплое staging) | 0011 |

## Публичный API

Публичное — то, что перечислено здесь; остальное — приватные детали модуля.

- `models.Tenant` — ORM-модель реестра тенантов (читают миграции, сиды,
  будущий онбординг; `tenants.config` напрямую не трогать — см. ниже).
- `config.TenantConfig`, `config.HotelProfile` — схема конфигурации тенанта:
  `schema_version`, профиль отеля, часовой пояс (`.tzinfo` — для показа
  локального времени, §9), язык по умолчанию.
- `config.load_tenant_config(session, tenant_id) -> TenantConfig` /
  `config.store_tenant_config(session, tenant_id, config)` — единственный
  путь чтения/записи конфига (P-12): только на нём гарантирована валидация
  схемой. Ошибки — `AppError` с кодами ERR-PLATFORM-004…006
  (docs/runbooks/errors.md).
- `config.TENANT_CONFIG_SCHEMA_VERSION` — текущая версия структуры конфига;
  повышается только при несовместимом изменении вместе со скриптом миграции
  конфигов всех тенантов (§6).
- `seed.seed_demo_tenant() -> uuid.UUID` — создать/дозаполнить демо-тенанта
  (идемпотентно); `seed.DEMO_TENANT_SLUG = "demo-hotel"`.

## События

- Публикует: `canary.created` (`CanaryCreated`) — демонстрационное событие
  канона; публикуется тестами и `hospitality/tools/publish_demo_event.py`
  (сквозная проверка конвейера на staging).
- Потребляет: `canary.created` — подписчик `echo_canary_created`
  (регистрируется composition root'ом воркера, `hospitality/worker.py`).

## Таблицы

- `tenants` (миграции `0001`, `0005`) — реестр тенантов: `id` (UUID), `slug`
  (уникальный человекочитаемый идентификатор), `name` (отображаемое имя —
  единственный источник, в конфиг не дублируется), `config` (JSONB, форма —
  `TenantConfig`; NULL = онбординг не завершён), `created_at`, `updated_at`.
  Таблица НЕ тенантная (это сам реестр), поэтому без `tenant_id`/RLS;
  RLS-канон для тенантных таблиц — Task 0009.
- `tenant_isolation_canary` (миграция `0002`) — канонический образец тенантной
  таблицы, якорь обязательного теста изоляции; в проде пуста.

## Зависимости

Внутренние: `hospitality.shared` (канон БД — `Base`, `UTCDateTime`, `utc_now`,
`platform_session_scope`; канон событий — `DomainEvent`, `publish`; канон
ошибок — `AppError`).
Направление kernel: `platform` → `shared`, обратное запрещено (import-linter).
Внешние сверх общих для проекта: нет.

## Типовые сценарии изменения

- Новое НЕобязательное поле конфига тенанта → поле со значением по умолчанию
  в `TenantConfig`/`HotelProfile` + тест валидации; `schema_version` не
  меняется.
- Несовместимое изменение конфига → повышение `TENANT_CONFIG_SCHEMA_VERSION`
  + скрипт миграции конфигов всех тенантов (§6; дисциплина как у Alembic) +
  обновить статью ERR-PLATFORM-006.
- Изменение демо-тенанта → `demo_tenant_config()` в `seed.py`; уже засеянные
  среды сид не перезапишет — на staging поправить конфиг руками через
  `store_tenant_config` или пересоздать тенанта.
- Новая колонка `tenants` → `models.py` + `alembic revision --rev-id NNNN
  --autogenerate` + этот README (раздел «Таблицы»).
- Новая тенантная таблица модуля → копия канона `TenantIsolationCanary`
  (модель) + RLS-блок в миграции по образцу `0002`.
