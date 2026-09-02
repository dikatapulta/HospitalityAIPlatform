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
| `models.py` | `RequestCategory`, `ServiceRequest` — тенантные таблицы (канон RLS Task 0009); `RequestStatus` — жизненный цикл; `ServiceRequestOrigin` — источник заявки |
| `service.py` | `create_category`, `create_request`, `change_request_status`, `get_request`, `list_requests`, `list_categories`, `find_open_requests_by_daily_number`, `list_open_requests_by_ids`, `list_unclaimed_requests`, `list_open_requests`, `list_requests_closed_since`, `anonymize_expired_request_texts`; карта переходов `STATUS_TRANSITIONS`; присвоение дневного номера; коды ошибок |
| `day_summary.py` | `day_summary(service_day)` + схемы `RequestsDaySummary` / `DayServiceCounts` — числа заявок за сутки отеля (spec 0035 §6). Отдельным файлом, а не в `service.py`: тот и без него за границей R-3 (~400 строк) |
| `events.py` | `RequestCreated`, `RequestStatusChanged` (канон событий Task 0010); `RequestInitiator` (spec 0025) |
| `schemas.py` | Pydantic-схемы границ: `*Create` на входе, `*Read` на выходе (R-6); страница списка `ServiceRequestPage`. `ServiceRequestRead` отдаёт `origin`, `claimed_at`, `closed_at` и `claimed_by_*`, но НЕ `closed_by_*` (spec 0035 §13: из пары «кто закрыл» наружу уходит только момент) |
| `router.py` | **CANONICAL ENDPOINT** (Task 0013): HTTP API `/api/v1/requests` поверх `service.py` |
| `tests/` | Жизненный цикл, публикация событий, изоляция тенантов, HTTP API, метки времени и «кто закрыл» (`test_measurability.py`), числа сводки дня (`test_day_summary.py`) |

## Публичный API (`api.py`)

- `create_request(ServiceRequestCreate) -> ServiceRequestRead` — заявка в
  статусе `new` + событие `request.created` в той же транзакции. Присваивает
  **дневной номер `#N`** (см. ниже). Требует **`origin`** — источник заявки
  (`ServiceRequestOrigin`: `guest_chat` | `staff_manual` | `api`, spec 0035 §4):
  поле обязательное и умолчания не имеет ни в схеме, ни в БД, потому что из
  доли `guest_chat` считается Exit-критерий Phase 1 — путь, забывший назвать
  источник, молча подмешался бы в измеряемое число. Принимает необязательный
  `guest_language` (ISO 639-1) — язык гостя для статусных уведомлений
  (spec 0021 П-1) — и `is_urgent` (умолчание `False`) — признак срочности
  (spec 0034 §5). На срочность модуль не смотрит: он её хранит и отдаёт,
  а решают по ней слои выше — AI снимает гейт подтверждения (ADR-018),
  канал маркирует уведомление службе, а ночную доставку ветвит issue #212.
  Отдельного статуса «срочная» нет намеренно: срочность ортогональна стадии
  работы (тот же довод, что в ADR-013).
- `change_request_status(request_id, RequestStatus, resolution_note=,
  initiator=, acting_user=) -> ServiceRequestRead` — переход по жизненному
  циклу + событие `request.status_changed`. `resolution_note` — примечание
  персонала к закрытию (частичное выполнение / причина отмены, spec 0021 П-4):
  пишется только на терминальном переходе, на прочих игнорируется с
  warning-логом. `initiator` (`RequestInitiator.GUEST|STAFF`, spec 0025) — кто
  инициировал переход; уезжает в событие как есть, `None` — не указан
  (существующие пути персонала). `acting_user` (`ActingUser`: user_id +
  display_name, spec 0033 §5) — кто действует из кабинета: на переходе
  `new → in_progress` заполняет `claimed_by_*`, на терминальном — `closed_by_*`;
  `None` (Telegram-суррогат, HTTP API, отмена гостем) — имена остаются пустыми.
  **Метки времени пишутся всегда, имена — только с личностью** (spec 0035 §3):
  `claimed_at` ставится на каждом переходе `new → in_progress`, `closed_at` — на
  каждом терминальном, из любого канала. Иначе медиана времени взятия считалась
  бы по одному кабинету и молча занижала объём работы, сделанной через Telegram
  (на пилоте оба пути живут одновременно). Отсюда предикат, который обязана
  сохранить любая правка карты переходов: `claimed_by_user_id IS NOT NULL` ⟹
  `claimed_at IS NOT NULL` (и то же у пары закрытия); обратное неверно — «взято,
  но некем» это норма данных. `closed_at` отвечает на «когда закрыли», а не
  «закрыта ли»: «заявка открыта» считается по статусу.
- `get_request(request_id) -> ServiceRequestRead`.
- `find_open_requests_by_daily_number(daily_number) -> list[ServiceRequestRead]`
  — незакрытые заявки тенанта с этим дневным номером (резолв команды `/done N`
  в staff-чате). Список, а не одна: номер за сутки может повториться, тогда
  вызывающая сторона просит уточнить (см. «Дневной номер»).
- `list_open_requests_by_ids(request_ids) -> list[ServiceRequestRead]` —
  незакрытые заявки тенанта среди переданных id, в порядке создания
  (spec 0025: опора снапшота «активные заявки диалога» — id передаёт канал из
  своей привязки `request_origins`; терминальные и чужие тенанту молча
  выпадают).
- `list_unclaimed_requests(created_before=, limit=) -> list[ServiceRequestRead]`
  — заявки тенанта, которые никто не взял: статус `new` и создана раньше
  границы, новые сверху, не больше `limit` (spec 0028: опора напоминаний о
  висящих заявках). Модуль не знает ни про сроки, ни про адресатов — порог
  берёт из конфига тенанта вызывающая сторона. Порядок «новые сверху» вместе с
  `limit` существен: срез не должен «съедаться» хвостом старых висяков.
- `list_open_requests(limit=) -> list[ServiceRequestRead]` — открытые заявки
  тенанта (`new` + `in_progress`), новые сверху (spec 0033 §5: лента очереди
  кабинета). Фильтры представления (категория, «мои») — забота вызывающей
  стороны; `limit` — страховка от неограниченного скана.
- `list_requests_closed_since(closed_after=, limit=) -> list[ServiceRequestRead]`
  — закрытые (`done`/`cancelled`) с `closed_at >= closed_after`, свежезакрытые
  сверху (spec 0033 §5: вкладка «закрытые за сегодня»). Момент закрытия берётся
  из `closed_at` (spec 0035 §3), а не угадывается по `updated_at`: джоба
  обезличивания #42 тоже бампает `updated_at`, и раньше древние заявки
  всплывали бы во вкладке в день её прогона — это чинилось отдельным условием
  по плейсхолдеру (ревью PR #154), теперь условие снято за ненадобностью.
  Терминальные строки, обезличенные ДО миграции `0025`, имеют пустой
  `closed_at` и не всплывают никогда. А вот заявка, провисевшая открытой дольше
  срока ретеншна и закрытая сегодня, во вкладку попадёт — с плейсхолдером
  вместо текста, и это намеренно: закрытие действительно случилось сегодня,
  прятать его значило бы разойтись со сводкой дня. Границу суток (полночь
  отеля) считает вызывающая сторона — модуль не знает про часовые пояса
  представления.
- `list_requests(limit=, offset=) -> ServiceRequestPage` — страница заявок
  тенанта, новые сверху (канон пагинации Task 0013).
- `anonymize_expired_request_texts(created_before=) -> int` — обезличить
  свободный текст заявок старше границы (issue #42, spec 0032): `summary` →
  `REQUEST_TEXT_ANONYMIZED_PLACEHOLDER`, `details`/`resolution_note` → NULL;
  агрегаты (статус, категория, комната, номер, времена) не трогаются — отчёты
  живут. Идемпотентна (P-8): повторный вызов обновляет 0 строк. Модуль не
  знает про срок — границу (90 дней политики) считает вызывающая сторона
  (`channels/common/retention.py` из цикла воркера).
- `day_summary(service_day) -> RequestsDaySummary` — числа заявок за один день
  отеля (spec 0035 §6, issue #300): создано (с разбивкой по **всем трём**
  значениям `origin`), закрыто (`done`/`cancelled`), разрез по службам, медиана
  времени взятия, просрочено за день, открыто сейчас. Границы дня — сутки
  отеля: «создано» сравнивается с `service_day`, «закрыто» и «взято» — с
  `closed_at`/`claimed_at` в окне `[полночь отеля, +1 день)`; окно отдаётся
  наружу полями `day_start`/`day_end`, чтобы остальные владельцы чисел сводки
  (эскалации `channels/common`, расход `ai/gateway`) считали своё ровно за него.
  `claim_median_seconds` и `overdue` равны `None`, когда числа НЕ СУЩЕСТВУЕТ
  (в этот день не брали; у отеля выключены напоминания) — на странице это
  прочерк, а не ноль. Разбивка `created_by_origin` заполнена всегда всеми
  значениями enum: сумма обязана сходиться с «создано», потому что «создано» —
  знаменатель Exit-критерия Phase 1, а `guest_chat` — его числитель (issue
  #313). Среза `limit` у функции нет намеренно: у счётчика урезанная выборка
  даёт не «показали не всё», а неверное число, и молча.
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
  old_status, new_status, initiator — аддитивное поле spec 0025/§13.5:
  `guest`/`staff`/None, кто инициировал переход; по нему подписчики выбирают
  адресата — гостя не извещают о его собственной отмене, персонал — извещают).
  Публикация — атомарно с бизнес-записью (P-6, outbox ADR-005).
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

## Таблицы (миграции `0006`, `0010`, `0012`, `0013`, `0018`, `0024`, `0025`; RLS — копия канона `0002`)

- `request_categories` — `id`, `tenant_id` (FK+индекс), `key`
  (уникален в паре с `tenant_id`), `name`, `created_at`, `updated_at`.
- `service_requests` — `id`, `tenant_id` (FK+индекс), `category_id`
  (FK+индекс), `status` (VARCHAR, значения `RequestStatus`), `summary`,
  `details`, `room_number`, `service_day` (DATE, NULL), `daily_number`
  (INT, NULL), `guest_language` (VARCHAR(2), NULL — ISO 639-1 язык гостя на
  момент создания, для статусных уведомлений, spec 0021 / миграция `0012`),
  `resolution_note` (VARCHAR(500), NULL — примечание персонала к закрытию,
  spec 0021 / миграция `0013`), `claimed_by_user_id` (UUID, NULL — FK на
  платформенных `users`, ondelete SET NULL) + `claimed_by_display_name`
  (VARCHAR(255), NULL — снапшот имени взявшего; PII сотрудника,
  docs/PII_REGISTRY.md; оба — spec 0033 §5 / миграция `0018`),
  `is_urgent` (BOOLEAN NOT NULL DEFAULT false — срочная заявка, spec 0034 §5 /
  миграция `0024`), `claimed_at` + `closed_at` (TIMESTAMPTZ, NULL — когда взяли
  и когда закрыли; пишутся из любого канала) и `closed_by_user_id` (UUID, NULL —
  FK на `users`, ondelete SET NULL) + `closed_by_display_name` (VARCHAR(255),
  NULL — снапшот имени закрывшего; PII сотрудника, docs/PII_REGISTRY.md),
  `origin` (VARCHAR(16) NOT NULL **без server_default** — источник заявки,
  значения `ServiceRequestOrigin`; все пятеро — spec 0035 §3–§4 / миграция
  `0025`), `created_at`, `updated_at`. Тройка
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
- **Новый вопрос к заявкам из фоновой задачи** — функция-запрос в `service.py` +
  строка в `api.py` (как `list_unclaimed_requests`); политика («через сколько
  считать просроченной», «кому слать») остаётся в композиционном слое.
- **Новый статус/переход** — значение в `RequestStatus`, ребро в
  `STATUS_TRANSITIONS`, миграция данных при необходимости, тесты переходов.
  Заодно решить судьбу меток времени: пары `claimed_*` и `closed_*` снимаются
  вместе (возврат заявки в `new` — issue #216 — обнуляет и время, и имя, иначе
  «взята в 06:10» висит у заявки, которую никто не берёт).
