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
| `config.py` | CANONICAL: конфигурация тенанта — схема `TenantConfig` со `schema_version` (§6) + `load_tenant_config`/`store_tenant_config`; `LocalizedText` и `RoomServiceConfig` (spec 0038) | 0011, #418 |
| `seed.py` | Идемпотентный сид демо-тенанта «Demo Hotel» (`make seed`; выполняется на каждом деплое staging) | 0011 |
| `auth.py` | Аутентификация HTTP (§11): звенья `TenantResolver` — сервисный токен (0013) и staff-сессия + slug кабинета (#48 PR C, ADR-008 §6) + FastAPI-зависимость канонического эндпоинта | 0013, #48 |
| `staff_auth.py` | Аутентификация персонала (spec 0033 §3, spec 0037 §3, ADR-008 §1): отель по коду (`find_hotel`), login/logout по логину отеля, сессии кабинета, `require_role` (в т.ч. fail-closed сверка RLS-контекста), деактивация | #48 PR B, #368 |
| `staff_credentials.py` | Креденшелы персонала (выделено из `staff_auth.py`, R-3, ревью #153): формат логина отеля и сборка/разбор `external_id` (spec 0037 §2, §6), argon2id-пароли, rate-limit входа по паре «отель + логин» и IP | #48 PR D, #368 |
| `staff_invites.py` | Приглашения сотрудников (spec 0033 §3.4, spec 0037 §4): выпуск с логином (формат и занятость в отеле) / отзыв / принятие одноразовой ссылки — всегда новый User, список ожидающих и описание ссылки для страницы принятия | #48 PR B/F, #368 |
| `staff_team.py` | Состав команды тенанта (spec 0033 §7): список членств с логином, службами и активностью, смена уровня и служб, отключение сотрудника; службы, у которых есть активный сотрудник (`staffed_service_keys` — лестница spec 0045 §4); выделено из `staff_auth.py` (R-3) | #48 PR F, #368, #472 |
| `staff_services.py` | Службы сотрудника ([spec 0045](../../../docs/specs/0045-services-and-levels.md) §2, §3, §6): ключи служб ресепшена, уборки (spec 0046 §3) и бухгалтерии (spec 0049 Р-1), `has_reception`, `has_housekeeping`, `sees_all_services` (ресепшен и менеджер — заявки всех служб), приведение списка служб к записи по категориям отеля (`normalize_service_keys`, `ERR-AUTH-015`) | #472, #476 |
| `staff_superadmin.py` | Суперадмин ([spec 0044](../../../docs/specs/0044-superadmin.md)): вход с кодом из приложения (`superadmin_login`), зависимость `require_superadmin`, ссылка нового пароля (`issue_password_reset`), стирание учётки (`erase_member`); каждое действие — событие `SuperadminActed` в той же транзакции | #377 |
| `staff_password_reset.py` | Анонимная дверь ссылки нового пароля (spec 0044 §4): показ (`describe_password_reset`) и принятие (`accept_password_reset`) — новый хэш той же идентичности, сессии сотрудника погашены | #377 |
| `totp.py` | Коды из приложения (TOTP, RFC 6238) на stdlib: секрет, код шага, проверка с окном ±1 шаг и отсечкой повтора, адрес `otpauth://` для QR (spec 0044 §9) | #377 |
| `superadmin_alerts.py` | Подписчик `SuperadminActed`: текст уведомления основателю (отель, время, роль — без логинов и имён) → алерт-бот `TELEGRAM_ALERT_*` (spec 0044 §6) | #377 |
| `staff_actions.py` | Журнал изменений кабинета ([spec 0041](../../../docs/specs/0041-staff-changes-log.md)): `StaffActor` (личность действия), запись строки в сессии действия, чтение дня для страницы, удаление по сроку для ретеншна | #370 |
| `files.py` | Файлы тенанта ([spec 0047](../../../docs/specs/0047-file-storage.md), ADR-022) — единственная дверь модулей к хранилищу: модель `UploadedFile`, `store_upload`, `read_file`, `delete_file`, ретеншн `delete_expired_files`, HTTP-выдача `file_response`; коды `ERR-FILES-001…006` | #491 |
| `staff_push.py` | Уведомления на телефон ([spec 0050](../../../docs/specs/0050-staff-web-push.md)) — единственная дверь к порту `WebPushPort`: модель `StaffPushSubscription`, запись подписки телефона за сотрудником (`save_subscription`, upsert по адресу, не больше 5 на сотрудника), удаление при выходе, проверочное уведомление, `deliver` — отправка и её последствия (`gone`/`rejected` удаляют подписку); коды `ERR-PUSH-001…003` | #477 |
| `file_sandbox.py` | Изоляция обработки (FOUNDATION §11, spec 0047 §4): `prepare_upload_isolated` — проверка файла в дочернем процессе с пустым окружением, лимитами памяти и CPU, таймаутом, не больше двух одновременно; ответ ребёнка перепроверяется; `extract_text_isolated` — текст PDF/DOCX тем же ребёнком (spec 0049 Р-4); `check_sandbox` — проба «жива ли песочница» | #491, #493, #511 |
| `file_text.py` | Текст документа гостя для разбора реквизитов ([spec 0049](../../../docs/specs/0049-act-requisites.md) Р-4): PDF — `pypdf`, ≤ 20 страниц; DOCX — `zipfile` + expat, документ с DTD — отказ; ≤ 100 000 знаков. Исполняется только в дочернем процессе песочницы | #493 |
| `field_encryption.py` | Шифрование полей БД (FOUNDATION §11, spec 0049 Р-3; маркер CANONICAL): `encrypt_field`, `decrypt_field`, `field_aad` — тот же ключ и формат, что у файлов, AAD — адрес значения; ключа нет — `ERR-FILES-005` | #493 |
| `file_content.py` | Содержимое загружаемого файла (spec 0047 §4): назначения `FilePurpose` и их правила, тип по байтам, перекодирование картинки в WebP без метаданных, отказ DOCX с макросами, AES-256-GCM объекта | #491 |
| `legal.py` | Публикация политики конфиденциальности: публичная страница `GET /legal/privacy` из `docs/legal/privacy-policy.md` + `privacy_policy_url()` для текста согласия гостя | spec 0029 |

## Публичный API

Публичное — то, что перечислено здесь; остальное — приватные детали модуля.

- `models.Tenant` — ORM-модель реестра тенантов (читают миграции, сиды,
  будущий онбординг; `tenants.config` напрямую не трогать — см. ниже).
- `config.TenantConfig`, `config.HotelProfile` — схема конфигурации тенанта:
  `schema_version`, профиль отеля, часовой пояс (`.tzinfo` — для показа
  локального времени, §9), язык по умолчанию, маршрутизация уведомлений по
  службам `staff_chats_by_category` (spec 0026), сроки напоминаний о невзятых
  заявках `request_reminder_after_minutes` / `…_minutes_by_category` (spec 0028),
  адресат и время утренней сводки `daily_summary_chat_id` /
  `daily_summary_local_time` (spec 0035 §8), справочник отеля `hotel_facts`
  (spec 0036 §3), контакты ресепшена для гостя `reception_phone` (spec 0027
  §3.1) и `reception_room_dial` — что набрать на телефоне в номере, только
  цифры (spec 0034 §3), служба эскалаций `escalation_category_key` — ключ
  категории, в которой ЧП и сбои бота заводят заявку (spec 0039 §2.3; `null` —
  только эскалация, как до спеки; задаёт онбординг `--escalation-category`).
- `TenantConfig.staff_chat_for(category_key, default=...)` — чат службы для
  категории заявки, фолбэк — дефолтный чат; `TenantConfig.staff_chat_ids(
  default=...)` — множество ВСЕХ чатов персонала тенанта (граница «кто
  персонал» в `channels/telegram/service.py`): дефолтный, чаты служб и чат
  утренней сводки — в группе руководства сидит тот же бот, и её реплай на
  сводку обязан быть командой персонала, а не репликой гостя (spec 0026 §4).
  Оба — чистые функции конфига,
  единственное место правила фолбэка (P-12). Задаёт маппинг
  `python -m hospitality.tools.staff_routing` (docs/runbooks/telegram.md).
- `TenantConfig.reminder_delay_for(category_key) -> timedelta | None` /
  `TenantConfig.min_reminder_delay() -> timedelta | None` — срок, после которого
  невзятая заявка подсвечивается напоминанием в чат службы (spec 0028, issue
  #57): пер-категорийный `request_reminder_minutes_by_category` перекрывает
  базовый `request_reminder_after_minutes` (по умолчанию 30 мин; `null` =
  напоминания выключены). Задаёт срок
  `python -m hospitality.tools.request_reminders`.
- `TenantConfig.daily_summary_chat_id` / `daily_summary_local_time` +
  `TenantConfig.daily_summary_at -> time` — утреннее сообщение со сводкой дня
  (issue #301, spec 0035 §8): чат менеджера (`null` = отелю не слать, страница
  кабинета работает; пустая строка запрещена) и время рассылки «HH:MM» по
  времени отеля (по умолчанию `09:00`). Свойство `daily_summary_at` — единственное
  место разбора строки в `time`, как `tzinfo` для пояса. Задаёт
  `python -m hospitality.tools.daily_summary`; онбординг оба поля переносит из
  прежнего конфига и сам не задаёт (id чата узнают, добавив бота в группу).
- `TenantConfig.category_hints` — типовые предметы службы (`key` категории →
  короткий список, до `MAX_CATEGORY_HINT_LENGTH` символов, issue #123). Читает
  AI-слой: список уходит в описание enum'а `create_service_request`, домен о
  подсказках не знает. Предмет, который выдают две службы («кофе в пакетиках» у
  housekeeping, сваренный — у fnb), намеренно называется у обеих: так модель
  видит неоднозначность и спрашивает гостя вместо угадывания. Задаёт
  `python -m hospitality.tools.onboard_tenant`
  (docs/runbooks/tenant-onboarding.md).
- `TenantConfig.hotel_facts: list[HotelFact]` + `config.HotelFact` — справочник
  отеля (spec 0036 §3, issue #211): «тема → ответ гостю», у временных фактов —
  `valid_until`. Уходит блоком в системный промпт КАЖДОГО хода, поэтому пределы
  бюджетные, а не вкусовые: `MAX_HOTEL_FACTS` = 30, `MAX_HOTEL_FACT_TOPIC_LENGTH`
  = 40, `MAX_HOTEL_FACT_ANSWER_LENGTH` = 300, `MAX_HOTEL_FACTS_TOTAL_CHARS` = 3000
  (арифметика занятого — `config.hotel_facts_total_chars(facts)`, одна на схему
  и на счётчик страницы кабинета). Список, а не словарь: JSONB переупорядочивает
  ключи объекта, а порядок здесь расставил отель. Идентичность факта — тема
  (уникальна без учёта регистра и крайних пробелов; «/» в теме запрещён). Дата
  в прошлом схемой НЕ отвергается намеренно — просроченный факт остаётся в
  конфиге, и схема, отвергающая прошлое, роняла бы весь конфиг отеля через день
  после срока; опечатку в годе ловит страница кабинета при сохранении.
- `TenantConfig.room_service: RoomServiceConfig | None` + `config.RoomServiceConfig`,
  `config.RoomServiceCatalogConfig` — room service отеля (spec 0038 Р-10): флаг
  `enabled`, валюта `KZT`, языки (`en` первый), у каталогов `restaurant` / `shop`
  (`RoomServiceCatalogKey`) служба `category_key`, сбор `service_charge_percent`
  (0–`MAX_ROOM_SERVICE_CHARGE_PERCENT`, до сотых) и `delivery_note`. Источник — файл
  профиля (`tools/onboard_tenant`; профиль без ключа прежнее значение переносит).
  `TenantConfig.room_service_catalog(catalog)` — единственное место правила «каталог
  открыт» (включён и есть в конфиге), по нему модуль `room_service` отвечает 404.
- `config.LocalizedText` — текст, который отель пишет сам на языках гостя (spec 0038
  Р-6): `en` обязателен, `ru` / `kk` — по желанию, пробельный перевод = нет перевода,
  чужой язык — ошибка; `text_for(language)` с откатом на английский, `as_json()` — форма
  JSONB. Берут конфиг (заметка о доставке) и модуль `room_service` (названия, описания).
- `TenantConfig.active_hotel_facts(now=None) -> tuple[HotelFact, ...]` — факты,
  которые сегодня видит гость, в порядке хранения: единственное место правила
  «просрочен» (P-12). Граница — по часовому поясу отеля, последний день
  включительно. Читает AI-слой (блок системного промпта) и страница кабинета
  (пометка «срок истёк — гости этого не видят»).
- `config.mutate_tenant_config(session, tenant_id, mutate) -> TenantConfig` —
  единственный примитив АТОМАРНОЙ правки конфига (spec 0036 §7): `SELECT … FOR
  UPDATE` на строку тенанта, применение функции «конфиг → конфиг», запись.
  Пара `load` + `store` для правки не годится — конфиг пишется целиком, и две
  одновременные правки затирают друг друга молча. Проверки, которым нужно
  актуальное состояние («тема уже есть», «места нет»), делаются внутри `mutate`:
  только там видно то же состояние, что и записи. Блокировка живёт до конца
  транзакции вызывающей стороны.
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
- `auth.resolve_tenant_from_service_token` — звено `TenantResolver` по
  `Authorization: Bearer <SERVICE_TOKEN>` для `TenantContextMiddleware`
  (цепочку `chain_resolvers` собирает composition root); невалидный токен
  неотличим от отсутствующего.
- `auth.resolve_tenant_from_staff_session` — звено `TenantResolver` кабинета
  (ADR-008 §6, spec 0033 §3.3): staff-cookie + slug из `/staff/{tenant_slug}/…`
  → активное членство → tenant_id, иначе None. Роль не проверяет (это
  `require_role` на странице); `/api/v1/*` со staff-cookie не резолвит.
  Служебные сегменты пути кабинета — `_STAFF_RESERVED_SEGMENTS` (новый
  сегмент не-slug под `/staff/` добавлять туда же).
- `auth.require_authenticated_tenant` — FastAPI-зависимость канонического
  эндпоинта (§11: «эндпоинт рождается аутентифицированным»): без контекста
  тенанта — 401 `ERR-PLATFORM-007`; заодно объявляет bearer-схему в OpenAPI.
- Staff-идентичность (spec 0033 §3, ADR-008 §1; потребители — страницы
  кабинета `staff_portal/` и звено резолвера в `auth.py`, PR C серии #48):
  - `models.User`, `models.StaffRole` (уровень: `staff|senior|manager`) и
    статусы — словарь staff-мира; уровень живёт на членстве, один на членство,
    рядом — службы `TenantMembership.service_keys` (ключи категорий заявок,
    spec 0045 §2; у менеджера пусто).
  - `staff_services.HOUSEKEEPING_SERVICE_KEY` (`housekeeping`), `has_housekeeping(keys)` —
    служба уборки ведёт путь номера после выезда (spec 0046 §4);
    `staff_services.ACCOUNTING_SERVICE_KEY` (`accounting`) — служба «Бухгалтерия»,
    ей уходит запрос реквизитов для АВР (spec 0049 Р-1).
  - `staff_services.RECEPTION_SERVICE_KEY` (`reception`), `has_reception(keys)`,
    `sees_all_services(role, keys)` — менеджер или служба ресепшена видит и
    ведёт заявки всех служб (spec 0045 §3), остальные — своих,
    `normalize_service_keys(role, keys, *, known_service_keys) -> list[str]` —
    правила про список служб: менеджеру пусто, сотруднику и старшему хотя бы
    одна служба отеля, без повторов, по алфавиту (`ERR-AUTH-015`, 422). Ключи
    категорий передаёт вызывающий — ядро модуль requests не импортирует.
  - `staff_auth.find_hotel(slug) -> StaffHotel | None` — отель по slug (он же
    «код отеля» на входе, spec 0037 §3); сравнение точное, введённый руками
    код к slug приводит страница.
  - `staff_auth.login(tenant_id, staff_login, password, *, client_ip) ->
    StaffSessionGrant` — вход по логину отеля (spec 0037 §3): формат логина
    до поиска (`ERR-AUTH-012`, бюджет не тратит), argon2id-проверка,
    rate-limit по паре «отель + логин» И IP (канон 0023,
    `ERR-AUTH-001/-005/-006`); токен показывается один раз, в БД — SHA-256.
    Бюджет попыток тратят только отказы ERR-AUTH-001: `staff_credentials`
    делит его на чтение (`enforce_login_rate_limit` до проверки пароля) и
    списание (`record_failed_login` после отказа) — issue #207. Адрес клиента
    вызывающий берёт каноном `shared/clientip.py`, а не из `request.client`.
  - `staff_auth.resolve_staff_session(token) -> ActiveStaffUser | None` —
    валидность сессии на каждом запросе (idle/absolute TTL из настроек);
    на нём построено звено `resolve_tenant_from_staff_session`.
  - `staff_auth.list_memberships(user_id) -> list[StaffMembership]` —
    активные членства для экрана выбора отеля (`/staff/`, spec 0033 §3.3).
  - `staff_auth.require_role(*roles, or_reception=False)` — фабрика
    FastAPI-зависимости страницы и JSON-действия кабинета (§11): cookie
    `STAFF_SESSION_COOKIE` → сессия (401 `ERR-AUTH-002`) → членство с уровнем
    из `roles` или, при `or_reception=True`, со службой ресепшена любого уровня
    по slug из пути (403 `ERR-AUTH-003`)
    → fail-closed сверка RLS-контекста запроса с тенантом действия (403,
    лог `staff.tenant_context_mismatch`; ревью #153). Возвращает
    `StaffContext` (actor для логов и `acting_user` доменных операций —
    `StaffContext.actor() -> StaffActor`; `login` — логин отеля этого
    тенанта, его читает `load_staff_context`, spec 0041 §2.3; `service_keys`,
    `is_reception`, `is_housekeeping` и `sees_all_services` — службы членства,
    spec 0045, spec 0046).
    Готовый контекст берёт из `scope["state"]` (ключ
    `STAFF_CONTEXT_STATE_KEY`) — его кладёт звено резолвера того же запроса
    (`staff_auth.load_staff_context`), поэтому сессия и членство не
    перечитываются повторно; фолбэк с полными запросами — для вызовов вне
    HTTP-конвейера.
  - `staff_auth.logout(token)`; `staff_auth.deactivate_user(user_id, *,
    actor_user_id)` — одна транзакция: статус, сессии, членства (DoD #48);
    `staff_credentials.hash_password`/`verify_password`/`normalize_login`/
    `require_login_format`/`password_external_id`/`parse_password_external_id`/
    `enforce_login_rate_limit` — канон паролей и логина отеля (их же
    используют бутстрап, инвайты и «Сотрудники»; `ERR-AUTH-006/-007/-012`
    живут там же). Формат `external_id` = `<tenant_id>:<LOGIN>` собирает и
    разбирает только эта пара функций.
  - `staff_invites.create_invite(tenant_id, role, name, staff_login, *,
    invited_by, service_keys=())/revoke_invite/accept_invite(token, *,
    password)` — одноразовая ссылка-приглашение (TTL из настроек,
    `ERR-AUTH-004`). Службы приходят уже приведёнными `normalize_service_keys`
    и при принятии переходят в членство как есть. Логин
    проверяется при выпуске: формат (`ERR-AUTH-012`, 422) и свобода в отеле
    среди учёток и ожидающих приглашений (`ERR-AUTH-012`, 409) — одна
    функция `is_login_taken(session, tenant_id, staff_login)`, её же зовёт CLI
    бутстрапа. Принятие всегда создаёт нового User + identity + membership;
    логин, занятый между выпуском и принятием, ловит UNIQUE → `ERR-AUTH-004`,
    и приглашение гасится. Приглашение без
    логина (до миграции 0028 или от старого образа) мертво, как истёкшее.
    `staff_invites.list_pending_invites(tenant_id)` — ожидающие ссылки для
    страницы «Сотрудники» (без токенов: в БД хэши);
    `staff_invites.describe_invite(token)` — куда и кем зовёт ссылка (или None
    на все невалидные исходы). Отзыв тенантно-скоупный: `revoke_invite(id, *,
    tenant_id, actor_user_id)` — чужой инвайт неотличим от несуществующего.
  - `staff_team.list_tenant_members(tenant_id)` / `change_member_access(user_id,
    tenant_id, role, *, service_keys, actor_user_id)` / `deactivate_member(user_id,
    tenant_id, *, actor_user_id)` — состав отеля для страницы «Сотрудники».
    `staff_team.staffed_service_keys(tenant_id) -> frozenset[str]` — службы
    с хотя бы одним активным сотрудником (живое членство, не отключённая
    учётка): заявку службы без сотрудников лестница spec 0045 §4 сразу
    поднимает к ресепшену и менеджеру.
    Все три тенантно-скоупные: цель обязана иметь активное членство В ЭТОМ
    отеле (`ERR-AUTH-008`), а свою учётку актор не трогает (`ERR-AUTH-011` —
    защита от самоблокировки последнего менеджера; принятие приглашения роль
    не меняет — оно всегда заводит новую учётку). Логин в строке — из
    идентичности этого отеля, учётка без него — `login=None`. Матрица прав —
    `docs/RBAC.md`.
  - `staff_actions.record_staff_action(session, actor, record)` — строка
    журнала изменений в сессии ДЕЙСТВИЯ (только `session.add`, своей
    транзакции нет: откат действия откатывает строку). Зовут `requests` и
    `guests`, только с личностью (`StaffActor`); путь без личности не пишет.
    Исключение — `record_automatic_action(session, record)` для действий
    системы (`AUTOMATIC_ACTIONS`: автовыезд по сроку, spec 0043 §2.3),
    `actor_*` = NULL, на странице — «автоматически».
    `list_staff_actions(*, day_start, day_end, actor_login, actions, limit,
    offset, category_ids=None)` и `list_staff_action_logins(*, category_ids=None)`
    — страница «Журнал изменений»; `category_ids` — граница старшего: строки, где
    служба «откуда» или «куда» из набора (spec 0045 §3);
    `delete_staff_actions_before(cutoff) -> int` — джоба ретеншна
    `channels/common`. `StaffAction` (значения колонки) реэкспортируется
    отсюда; подписи — у страницы.
  - Бутстрап первого менеджера — CLI `python -m
    hospitality.tools.staff_bootstrap <ЛОГИН> --name "Имя" --tenant-slug <код>`
    (пароль — getpass; дальше только приглашения из кабинета).
- Файлы ([spec 0047](../../../docs/specs/0047-file-storage.md); потребители —
  room service #418, документы #493, ретеншн `channels/common`). Всё — в
  контексте тенанта:
  - `files.store_upload(purpose, data, original_name, *, uploaded_by_user_id=None,
    guest_identity_id=None) -> UploadedFileRead` — проверка содержимого,
    шифрование, `put` в хранилище через порт, строка `uploaded_files`; отказ —
    `ERR-FILES-001…003`, нет ключа — `005`, хранилище упало — `006`.
  - `files.read_file(file_id) -> StoredFile` (метаданные + байты) и
    `files.delete_file(file_id)`; нет или чужой — `ERR-FILES-004`.
  - `files.file_response(stored)` — HTTP-ответ: `nosniff`, `CSP: sandbox`,
    PDF/DOCX вложением, файлы гостя без кэша. Маршрут выдачи с проверкой права
    пишет потребитель.
  - `files.delete_expired_files(purpose, *, created_before) -> int` — ретеншн.
  - `FilePurpose` (`menu_photo`, `guest_document`, `request_photo`) —
    реэкспорт из `file_content`. Порт хранилища и адаптеры потребитель не видит.
  - `file_sandbox.extract_text_isolated(data) -> ExtractedText` — текст PDF/DOCX
    в дочернем процессе (spec 0049 Р-4; потребитель — воркер модуля documents):
    `text` / `no_text` (скан) / `failed` (битый файл, DTD, таймаут); ребёнок умер —
    `SandboxFailedError`. `file_sandbox.check_sandbox()` — проба на пустом входе:
    молчит, если песочница жива, иначе `SandboxFailedError` — так потребитель
    отличает файл, убивший ребёнка лимитом, от сломанной среды (#511). От stderr
    ребёнка родитель держит только хвост.
- Шифрование полей БД (`field_encryption`, spec 0049 Р-3; первый потребитель —
  модуль documents): `encrypt_field(plaintext, aad) -> bytes`,
  `decrypt_field(blob, aad) -> str`, `field_aad(tenant_id, table, row_id, column)`.
  Ключ — `FILE_ENCRYPTION_KEY`; нет или не тот, шифртекст из чужой строки —
  `ERR-FILES-005` (503).
- Уведомления на телефон ([spec 0050](../../../docs/specs/0050-staff-web-push.md);
  потребители — `staff_portal/push_api.py`, рассылка воркера — PR 2). Всё — в
  контексте тенанта, порт `WebPushPort` потребитель не видит:
  - `staff_push.save_subscription(user_id, endpoint, p256dh, auth)` — upsert по
    `(tenant_id, endpoint)`, сверх 5 на сотрудника — вытесняется давняя; адрес не
    службы пуша или ключи не той формы — 422 `ERR-PUSH-001`.
  - `staff_push.delete_subscription(user_id, endpoint) -> bool` — «Выйти».
  - `staff_push.send_test(user_id, endpoint, *, url) -> PushOutcome | None` —
    проверочное уведомление на свой телефон; `None` — такой подписки нет.
  - `staff_push.deliver(subscription, message, *, ttl_seconds) -> PushOutcome` —
    одна отправка: `gone`/`rejected` удаляют подписку (`ERR-PUSH-002`),
    `failed` — `ERR-PUSH-003`, `delivered` ставит `last_delivered_at`. Сеть —
    вне транзакций (ADR-016). `PushMessage` — `{title, body, url, tag}` без
    текста гостя.
- Суперадмин ([spec 0044](../../../docs/specs/0044-superadmin.md); потребители —
  `staff_portal/` и CLI `tools/superadmin`):
  - `staff_superadmin.superadmin_login(tenant_id, login, password, code, *,
    client_ip) -> SuperadminSessionGrant` — своя дверь: логин отеля + пароль +
    код из приложения одним ответом `ERR-AUTH-001`, бюджет попыток общий с
    `staff_auth.login`, сессия на `STAFF_SUPERADMIN_SESSION_TTL_MINUTES`.
    `staff_auth.login` учётку с `is_platform_admin` не пускает.
  - `staff_superadmin.require_superadmin` — FastAPI-зависимость: `require_role
    (manager)` + флаг (иначе 403 `ERR-AUTH-003`); флаг перепроверяется в
    транзакции действия.
  - `staff_superadmin.issue_password_reset(user_id, tenant_id, *,
    actor_user_id) -> PasswordResetGrant` / `erase_member(user_id, tenant_id,
    *, actor_user_id)` — цель обязана иметь членство в отеле (`ERR-AUTH-008`),
    не своя (`ERR-AUTH-011`), не суперадмин (`ERR-AUTH-013`).
  - `staff_password_reset.describe_password_reset(token)` /
    `accept_password_reset(token, *, password)` — анонимная дверь ссылки;
    невалидная — `ERR-AUTH-014`.
  - `staff_team` не даёт менеджеру сменить роль или отключить суперадмина
    (`ERR-AUTH-013`) и не показывает стёртых (`TenantMemberView.
    is_platform_admin` — пометка строки).
  - `totp.generate_secret / code_at / matching_step / provisioning_uri` —
    TOTP; `superadmin_alerts.build_superadmin_alert_handler(alert_sender)` —
    подписчик, регистрирует воркер.

## События

- Публикует: `canary.created` (`CanaryCreated`) — демонстрационное событие
  канона; публикуется тестами и `hospitality/tools/publish_demo_event.py`
  (сквозная проверка конвейера на staging). `staff.superadmin_acted`
  (`SuperadminActed`) — вход и действия суперадмина, в той же транзакции, что
  действие (spec 0044 §6); нагрузка без логинов и имён.
- Потребляет: `canary.created` — подписчик `echo_canary_created`;
  `staff.superadmin_acted` — `superadmin_alerts` (уведомление основателю).
  Оба регистрирует composition root воркера (`hospitality/worker.py`).

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
  `secret_hash` — argon2id), `tenant_memberships` (уровень и службы живут здесь,
  `service_keys VARCHAR(63)[]` — миграция `0033`; UNIQUE user+tenant), `staff_sessions` (opaque-токен, в БД SHA-256; idle/absolute
  TTL), `staff_invites` (одноразовые приглашения, в БД SHA-256 токена; логин
  сотрудника — колонка `login`, миграция `0028`; службы — `service_keys`,
  миграция `0033`). У `kind=password`
  `external_id` = `<tenant_id>:<LOGIN>` (spec 0037 §6): уникальность логина в
  отеле даёт UNIQUE `(kind, external_id)`; email-идентичности миграция `0028`
  удалила. PII: `users.display_name`, `user_identities.external_id` (логин),
  `staff_invites.invited_name`, `staff_invites.login` — docs/PII_REGISTRY.md.
- Суперадмин (миграция `0032`, spec 0044 §7) — тоже платформенный мир без
  RLS и без `tenant_id`: `staff_second_factors` (секрет кода из приложения —
  открытым, хэшировать нельзя; шаг последнего принятого кода),
  `staff_password_resets` (ссылки нового пароля: идентичность, SHA-256 токена,
  кто выдал, срок, `used_at`). `users.status = 'erased'` — стёрт суперадмином
  (VARCHAR без CHECK, схема не менялась).
- `staff_action_log` (миграции `0030`, `0031` и `0034`, spec 0041, 0042 и 0045) — журнал изменений кабинета,
  тенантная таблица по канону `TenantIsolationCanary` с RLS-блоком 0002. Кто
  (`actor_user_id` FK SET NULL + снапшот `actor_login`), когда, что
  (`action`), над чем — снапшоты `#N`/дня/службы/комнаты, `request_id`/
  `stay_id` без FK. Пары «откуда/куда»: комнаты переселения
  (`room_number`/`room_number_to`) и службы передачи заявки
  (`category_name`/`category_name_to`, spec 0042) и их id `category_id`/`category_id_to`
  без FK — граница старшего (spec 0045 §3; строки до `0034` — NULL). Текстов гостя и имён нет по построению. Индекс
  `(tenant_id, occurred_at DESC)`. Срок — как у текстов гостей (spec 0032),
  удаляет джоба ретеншна. PII: `actor_login`.

- `uploaded_files` (миграция `0036`, spec 0047) — метаданные файлов в
  хранилище, тенантная таблица по канону `TenantIsolationCanary` с RLS-блоком
  0002: назначение, `storage_key` (`{tenant}/{purpose}/{uuid}.{ext}`,
  UNIQUE), тип, размер, sha256, `original_name` (PII), `uploaded_by_user_id`
  FK SET NULL, `guest_identity_id` без FK. Сам файл — шифртекст на томе.
  Индекс `(tenant_id, purpose, created_at)` — ретеншн.

- `staff_push_subscriptions` (миграция `0038`, spec 0050 §4) — подписки телефонов
  персонала на веб-пуш, тенантная таблица по канону `TenantIsolationCanary` с
  RLS-блоком 0002: `user_id` (FK CASCADE — стирание учётки удаляет подписки),
  `endpoint` (адрес службы пуша, PII — идентификатор телефона), ключи браузера
  `p256dh`/`auth` (base64url), `last_delivered_at`. UNIQUE `(tenant_id, endpoint)` —
  телефон под другим логином переходит к новому сотруднику; индекс `(tenant_id, user_id)`.

## Зависимости

Внутренние: `hospitality.shared` (канон БД — `Base`, `UTCDateTime`, `utc_now`,
`platform_session_scope`; канон событий — `DomainEvent`, `publish`; канон
ошибок — `AppError`; канон rate-limit — `consume_rate_limit`; метрики —
`record_staff_login`).
Направление kernel: `platform` → `shared`, обратное запрещено (import-linter).
Внешние сверх общих для проекта: `argon2-cffi` (хэш пароля сотрудника,
spec 0033 §3.1); `pillow` и `cryptography` (проверка и перекодирование
картинки, AES-256-GCM файлов — spec 0047). Хранилище файлов — через порт
`shared/ports/file_storage.py`; адаптер кладёт composition root. TOTP суперадмина — stdlib (`hmac`, `hashlib`), без
зависимости (spec 0044 §9).

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
