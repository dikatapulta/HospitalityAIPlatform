# showroom — 3D-витрина отеля: сцена здания, подбор номеров, код доступа, запросы посетителей

## Назначение

3D-витрина для посетителей без проживания — турагентов, корпоративных клиентов и частных
гостей ([spec 0069](../../../../docs/specs/0069-sales-showroom-3d.md), [ADR-025](../../../../docs/adr/ADR-025-public-showroom-without-login.md),
issue #799). Модуль владеет **сценой** — зданием отеля числами (корпус, стороны и виды, типы
номеров, этажи с номерами и помещениями), кодом доступа к странице посетителя и **запросами
посетителей** (`showroom_requests`, PR (в) #805). Модель помощника — PR (б) в `ai/showroom/`;
модуль модели не знает (FOUNDATION §5.1). Зависимость односторонняя: `showroom` →
`requests.api` (заявка запроса), как у `table_booking`.

**Запрос — не бронь** (P-9, NG-2, NG-3): наличие, цену и бронь подтверждает человек в Opera;
своей системы бронирования и оплаты нет, исходящей почты посетителю нет. Запрос живёт заявкой
(§2.4): номер `#N`, очередь службы, сигнал, пуш, лестница — у заявки (`origin=showroom_form`, вне
доли Exit-критерия, spec 0035 §4). Контакты — только в модуле, со своим сроком 365 дней (§3); в
тексте заявки — вид запроса, даты, гостей, компания, номера или вид.

Имён, занятости и цен из PMS в сцене нет и быть не может (NG-2): единственное число о деньгах —
`price_note` типа номера, ориентир, который задал сам отель. Наличие на странице — никогда.

## Состав

| Файл | Что даёт |
| --- | --- |
| `scene.py` | Схема сцены `SceneDocument` (`extra="forbid"`, ≤ 512 КБ, ≤ 2000 номеров) и `parse_scene` — размер, JSON, схема, ссылки между частями; отказ — 422 `ERR-SHOWROOM-002` «путь — причина» |
| `models.py` | `ShowroomScene` (миграция 0059) — тенантная, RLS-канон, одна строка на отель (`tenant_id` UNIQUE), `scene` JSONB, `scene_version`, кто и когда заменил. ПД нет |
| `schemas.py` | `SceneRead`, `RoomQuery` (этажи, сторона, вид, тип, `count` 1–60), `RoomMatch` |
| `service.py` | Настройки витрины из конфига, сцена (чтение, замена с версией и журналом), подбор `find_rooms`/`match_rooms`, код доступа (bcrypt-хэш, версия) и подпись cookie; коды 001, 003, 005 |
| `request_models.py` | `ShowroomRequest` (миграция 0060) — тенантная, RLS-канон; `ShowroomRequestKind` (`group`, `booking`). Снимок названий желаемого вида и типа (`wish_*_names`) — отель заменит сцену, а запрос живёт; `request_created` — заявка есть (Р-3 spec 0038). ПД — PII_REGISTRY |
| `request_schemas.py` | `ShowroomRequestCreate` (группа 1–500 гостей и до 60 номеров, гость — до 3 номеров, телефон или почта обязательны, у группы — компания) → `ShowroomRequestRead`, `ShowroomRequestPlaced`; `StaffShowroomRequestRead` (кабинет); `CONTACTS_RETENTION_DAYS = 365` |
| `request_scene.py` | Что запрос берёт из сцены — номера, виды, типы (`SceneFacts` из `get_scene`) |
| `visitor_requests.py` | «Отправить запрос» (`create_request`): проверки, строка с контактами, заявка в службе продаж (группа) или бронирования (гость; службы нет — продажи); суть и `details` заявки по-русски; код 004 |
| `request_staff_view.py` | Запросы для кабинета (`requests_for_requests`) и ветка ретеншна (`anonymize_expired`) |
| `api.py` | Публичный интерфейс (единственная точка входа извне) |
| `tests/` | Схема и пути отказов, подбор и порядок, версия и строка журнала, изоляция тенантов, код доступа: хэш, проверка, смена гасит прежнюю подпись, короткий код; запросы — службы, `origin` и текст заявки без контактов, повтор ключа и сбой с повтором, 004 и правила схемы, 001, маскирование карты, сводка дня, карточка из модуля, изоляция, обезличивание через 365 дней, брошенная строка (`request_fixtures.py`, фикстура `scene`) |

## Публичный API

Каждая функция — внутри `tenant_context`.

- `load_settings() -> ShowroomConfig | None` — `TenantConfig.showroom`; конфига нет — None.
- `has_scene()`, `get_scene() -> SceneRead | None`.
- `parse_scene(text) -> SceneDocument` — вход сцены снаружи (кабинет, демо-сид).
- `replace_scene(document, *, acting_user)` — заменить целиком: версия +1 под `FOR UPDATE`;
  с личностью — строка журнала `showroom_scene_replaced` (spec 0041), без неё (сид) — нет.
- `find_rooms(RoomQuery) -> list[RoomMatch]` и чистая `match_rooms(scene, query)` — номера под
  условия; порядок — этаж сверху вниз, затем номер; вид номера — его `view_keys`, пусто — виды его
  стороны (`room_views`). Тот же подбор у панели страницы и у команды помощника `FindRooms` (P-5).
- `set_access_code(code) -> int` — хэш в конфиг, версия +1 (витрины в конфиге нет — `ValueError`);
  `check_access_code_format` — 8–64 знака, ≤ 72 байт (предел bcrypt), без крайних пробелов;
  `verify_access_code(code) -> bool`.
- `access_token(settings, tenant_id)` / `token_matches(...)` — значение cookie доступа: HMAC-SHA256
  над `tenant_id:access_code_version`, ключ — SHA-256 от метки и bcrypt-хэша кода. Отдельного
  секрета деплоймента нет: соль хэша случайна, хэш лежит только в БД, смена кода меняет и ключ, и
  версию — прежние cookies гаснут. Сравнение — `hmac.compare_digest`.

- `create_request(ShowroomRequestCreate, *, consent_version) -> ShowroomRequestPlaced` — по
  порядку: повтор ключа (`client_key`) — тот же запрос (`duplicate=True`), заявка доводится,
  если в прошлый раз не создалась (Р-3); витрина выключена, сцены нет, служба не найдена среди
  категорий (001); заезд раньше сегодня отеля, выезд не позже заезда или дальше 400 дней, гостей
  брони больше `booking_max_guests`, номер, вид или тип не из сцены (004). Затем строка с
  `request_id = uuid4()` и `requests.create_request(request_id=…)`: заявка без комнаты, суть —
  «Групповой запрос · компания · N гост. · 12–15 марта» или «Запрос на бронирование · даты · N
  гост. · номер или вид», `details` — «Номера: …» или «Желание: вид «Горы», тип «Делюкс»».
  Номер карты в пожеланиях маскируется; согласие — версия от канала и время записи.
- `requests_for_requests(request_ids) -> dict[request_id, StaffShowroomRequestRead]` — запросы
  среди уже прочитанных заявок (очередь, сигнал); строка без заявки не видна.
- `anonymize_expired(now) -> ShowroomRetention` — компания, контакты и пожелания старше 365 дней
  → NULL, `anonymized_at`; строка без заявки старше суток удаляется. Идемпотентна; зовёт
  ретеншн (`channels/common/retention.py`, шаг 12).
- `date_range_ru`, `wish_ru` — подписи дат и желания (суть заявки и карточка кабинета).

## Настройки

`TenantConfig.showroom` — `platform/showroom_config.py` (§2.5): `enabled` (кабинет), `access_code_hash`
и `access_code_version` (страница по коду; ставит только команда
`python -m hospitality.tools.showroom_access_code set --tenant <slug>`), `public_enabled` (после
допсоглашения, PR г), службы `sales_category_key` и `booking_category_key`, `booking_max_guests`,
`assistant_enabled`, `contact_note`. Онбординг берёт витрину из профиля, а код всегда переносит из
прежнего конфига и задать его файлом не даёт.

## Таблицы

`showroom_scenes` — миграция 0059, `showroom_requests` — 0060; RLS по канону 0002, FK в чужие
модули нет. Контакты `showroom_requests` — [PII_REGISTRY](../../../../docs/PII_REGISTRY.md), по
обращению — рунбук subject-requests §3.7.

## Зависимости

`platform` (конфиг, журнал изменений), `shared`, `requests.api` (заявка запроса). Модуль не
импортирует `ai/`, `channels/`, `integrations/`. Потребители: `channels/showroom` (страница
посетителя, формы запроса), `staff_portal` (раздел «3D-витрина», карточка запроса в очереди,
сигнал), `channels/common/retention.py`, `tools/showroom_access_code`, `tools/demo_showroom`.

## Наблюдаемость (запросы)

Логи `showroom_request_created` (id запроса и заявки, вид, гостей, число номеров),
`showroom_category_missing` (ERROR, ключ службы), `showroom_requests_retention`; контактов,
компании и пожеланий в логах нет. Метрика `showroom_requests_total{tenant_id, kind}`.

## Типовые сценарии изменения

- Новое поле сцены — в `scene.py` с умолчанием (старые сцены в БД обязаны читаться), проверка
  ссылок — в `check_references`, просмотрщик — `channels/showroom/assets/viewer.js`.
- Здание не прямоугольной формы — файл модели вместо параметров (ADR-025 «Пересмотреть»).
