# platform — тенанты, пользователи, staff-идентичность, конфигурация (kernel)

<!-- CANONICAL: первый полный паспорт модуля (R-4, Task 0011). Новые модули
копируют СТРУКТУРУ этого файла: назначение → состав → публичный API →
события → таблицы → зависимости → типовые сценарии изменения. -->

## Назначение

Корневой модуль платформы: реестр тенантов и их конфигурация; пользователи
(staff-идентичность ADR-008 §1: User/членства/сессии/инвайты — spec 0033,
серия #48); в следующих задачах — RBAC v1, аудит, фиче-флаги, лимиты на
тенанта (FOUNDATION §5.1, ADR-003). Слой kernel: доменные модули (`modules/`)
опираются на `platform`, обратное запрещено.

## Состав

| Файл | Что даёт | Задача |
| --- | --- | --- |
| `models.py` | `Tenant` — единица изоляции данных и конфигурации (GLOSSARY); `TenantIsolationCanary` — канонический образец тенантной таблицы | 0008/0009 |
| `events.py` | CANONICAL: `CanaryCreated` + `echo_canary_created` — образец доменного события и идемпотентного подписчика (P-6, P-8) | 0010 |
| `config.py` | CANONICAL: конфигурация тенанта — схема `TenantConfig` со `schema_version` (§6) + `load_tenant_config`/`store_tenant_config` | 0011 |
| `seed.py` | Идемпотентный сид демо-тенанта «Demo Hotel» (`make seed`; выполняется на каждом деплое staging) | 0011 |
| `auth.py` | Аутентификация HTTP API сервисным токеном (§11): резолвер тенанта для middleware + FastAPI-зависимость канонического эндпоинта | 0013 |
| `staff_auth.py` | Аутентификация персонала (spec 0033 §3, ADR-008 §1): login/logout, сессии кабинета, `require_role`, деактивация | #48 PR B |
| `staff_invites.py` | Приглашения сотрудников (spec 0033 §3.4): выпуск/отзыв/принятие одноразовой ссылки | #48 PR B |
| `legal.py` | Публикация политики конфиденциальности: публичная страница `GET /legal/privacy` из `docs/legal/privacy-policy.md` + `privacy_policy_url()` для текста согласия гостя | spec 0029 |

## Публичный API

Публичное — то, что перечислено здесь; остальное — приватные детали модуля.

- `models.Tenant` — ORM-модель реестра тенантов (читают миграции, сиды,
  будущий онбординг; `tenants.config` напрямую не трогать — см. ниже).
- `config.TenantConfig`, `config.HotelProfile` — схема конфигурации тенанта:
  `schema_version`, профиль отеля, часовой пояс (`.tzinfo` — для показа
  локального времени, §9), язык по умолчанию, маршрутизация уведомлений по
  службам `staff_chats_by_category` (spec 0026), сроки напоминаний о невзятых
  заявках `request_reminder_after_minutes` / `…_minutes_by_category` (spec 0028).
- `TenantConfig.staff_chat_for(category_key, default=...)` — чат службы для
  категории заявки, фолбэк — дефолтный чат; `TenantConfig.staff_chat_ids(
  default=...)` — множество ВСЕХ чатов персонала тенанта (граница «кто
  персонал» в `channels/telegram/service.py`). Оба — чистые функции конфига,
  единственное место правила фолбэка (P-12). Задаёт маппинг
  `python -m hospitality.tools.staff_routing` (docs/runbooks/telegram.md).
- `TenantConfig.reminder_delay_for(category_key) -> timedelta | None` /
  `TenantConfig.min_reminder_delay() -> timedelta | None` — срок, после которого
  невзятая заявка подсвечивается напоминанием в чат службы (spec 0028, issue
  #57): пер-категорийный `request_reminder_minutes_by_category` перекрывает
  базовый `request_reminder_after_minutes` (по умолчанию 30 мин; `null` =
  напоминания выключены). Задаёт срок
  `python -m hospitality.tools.request_reminders`.
- `config.list_configured_tenant_ids(session) -> list[uuid.UUID]` — тенанты с
  завершённым онбордингом (конфиг задан). Опора фоновых задач: у них нет
  входящего запроса, тенантов они обходят сами и работают под `tenant_context`
  каждого (P-4). Сессия — платформенная.
- `config.load_tenant_config(session, tenant_id) -> TenantConfig` /
  `config.store_tenant_config(session, tenant_id, config)` — единственный
  путь чтения/записи конфига (P-12): только на нём гарантирована валидация
  схемой. Ошибки — `AppError` с кодами ERR-PLATFORM-004…006
  (docs/runbooks/errors.md).
- `config.load_tenant_name(session, tenant_id) -> str` — отображаемое имя отеля
  (`tenants.name` — единственный источник). Нужно поверхностям, которые говорят
  с гостем от лица отеля: приветствие консьержа (issue #39). Сессия —
  платформенная.
- `legal.privacy_policy_url() -> str` — абсолютный URL политики
  (`PUBLIC_BASE_URL` + `/legal/privacy`); её требует обязательная строка текста
  согласия гостя (spec 0029 §2). `legal.router` подключает composition root:
  страница публичная и вне контекста тенанта — явное решение §11, как `/health`
  (оператор обработки один на инсталляцию, политика у него одна). Документ не
  найден → `ERR-PLATFORM-008`.
- `config.TENANT_CONFIG_SCHEMA_VERSION` — текущая версия структуры конфига;
  повышается только при несовместимом изменении вместе со скриптом миграции
  конфигов всех тенантов (§6).
- `seed.seed_demo_tenant() -> uuid.UUID` — создать/дозаполнить демо-тенанта
  (идемпотентно); `seed.DEMO_TENANT_SLUG = "demo-hotel"`.
- `auth.resolve_tenant_from_service_token` — резолвер тенанта по
  `Authorization: Bearer <SERVICE_TOKEN>` для `TenantContextMiddleware`
  (подключает composition root); невалидный токен неотличим от отсутствующего.
- `auth.require_authenticated_tenant` — FastAPI-зависимость канонического
  эндпоинта (§11: «эндпоинт рождается аутентифицированным»): без контекста
  тенанта — 401 `ERR-PLATFORM-007`; заодно объявляет bearer-схему в OpenAPI.
- Staff-идентичность (spec 0033 §3, ADR-008 §1; в PR B серии #48 никем не
  вызывается — кабинет подключит в PR C):
  - `models.User`, `models.StaffRole` (`staff|receptionist|manager`) и статусы —
    словарь staff-мира; роль живёт на членстве, одна на членство.
  - `staff_auth.login(email, password, *, client_ip) -> StaffSessionGrant` —
    вход: argon2id-проверка, rate-limit по email И IP (канон 0023,
    `ERR-AUTH-001/-005/-006`); токен показывается один раз, в БД — SHA-256.
  - `staff_auth.resolve_staff_session(token) -> ActiveStaffUser | None` —
    валидность сессии на каждом запросе (idle/absolute TTL из настроек);
    контракт резолвера для третьего звена `TenantResolver` (PR C).
  - `staff_auth.require_role(*roles)` — фабрика FastAPI-зависимости страницы
    кабинета (§11): cookie `STAFF_SESSION_COOKIE` → сессия (401
    `ERR-AUTH-002`) → членство+роль по slug из пути (403 `ERR-AUTH-003`);
    возвращает `StaffContext` (actor для логов и `acting_user` PR D).
  - `staff_auth.logout(token)`; `staff_auth.deactivate_user(user_id, *,
    actor_user_id)` — одна транзакция: статус, сессии, членства
    (DoD #48); `staff_auth.hash_password`/`verify_password`/`normalize_email` —
    канон паролей и email-логина (их же использует бутстрап и инвайты).
  - `staff_invites.create_invite/revoke_invite/accept_invite` — одноразовая
    ссылка-приглашение (TTL из настроек, `ERR-AUTH-004`); принятие создаёт
    User + identity + membership, существующий email доказывает владение
    паролем и получает только membership (сеть отелей).
  - Бутстрап первого менеджера — CLI `python -m
    hospitality.tools.staff_bootstrap <email> --name "Имя"` (пароль — getpass;
    дальше только приглашения из кабинета).

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
- Staff-идентичность (миграция `0017`, spec 0033 §3.1) — платформенный мир
  ВНЕ RLS (ADR-008 §6; whitelist-исключения P-4 — docstring миграции):
  `users` (личность, без tenant_id), `user_identities` (способы входа;
  `secret_hash` — argon2id), `tenant_memberships` (роль живёт здесь; UNIQUE
  user+tenant), `staff_sessions` (opaque-токен, в БД SHA-256; idle/absolute
  TTL), `staff_invites` (одноразовые приглашения, в БД SHA-256 токена).
  PII: `users.display_name`, `user_identities.external_id` (email),
  `staff_invites.invited_name` — docs/PII_REGISTRY.md.

## Зависимости

Внутренние: `hospitality.shared` (канон БД — `Base`, `UTCDateTime`, `utc_now`,
`platform_session_scope`; канон событий — `DomainEvent`, `publish`; канон
ошибок — `AppError`; канон rate-limit — `consume_rate_limit`; метрики —
`record_staff_login`).
Направление kernel: `platform` → `shared`, обратное запрещено (import-linter).
Внешние сверх общих для проекта: `argon2-cffi` (хэш пароля сотрудника,
spec 0033 §3.1).

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
