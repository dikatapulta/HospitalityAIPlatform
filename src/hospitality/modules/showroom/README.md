# showroom — 3D-витрина отеля: сцена здания, подбор номеров, код доступа

## Назначение

3D-витрина для посетителей без проживания — турагентов, корпоративных клиентов и частных
гостей ([spec 0069](../../../../docs/specs/0069-sales-showroom-3d.md), [ADR-025](../../../../docs/adr/ADR-025-public-showroom-without-login.md),
issue #799). Модуль владеет **сценой** — зданием отеля числами (корпус, стороны и виды, типы
номеров, этажи с номерами и помещениями) — и кодом доступа к странице посетителя. Запросы
посетителей (`showroom_requests`) добавляет PR (в) серии, модель помощника — PR (б) в `ai/showroom/`;
модуль модели не знает (FOUNDATION §5.1).

Имён, занятости и цен из PMS в сцене нет и быть не может (NG-2): единственное число о деньгах —
`price_note` типа номера, ориентир, который задал сам отель. Наличие на странице — никогда.

## Состав

| Файл | Что даёт |
| --- | --- |
| `scene.py` | Схема сцены `SceneDocument` (`extra="forbid"`, ≤ 512 КБ, ≤ 2000 номеров) и `parse_scene` — размер, JSON, схема, ссылки между частями; отказ — 422 `ERR-SHOWROOM-002` «путь — причина» |
| `models.py` | `ShowroomScene` (миграция 0059) — тенантная, RLS-канон, одна строка на отель (`tenant_id` UNIQUE), `scene` JSONB, `scene_version`, кто и когда заменил. ПД нет |
| `schemas.py` | `SceneRead`, `RoomQuery` (этажи, сторона, вид, тип, `count` 1–60), `RoomMatch` |
| `service.py` | Настройки витрины из конфига, сцена (чтение, замена с версией и журналом), подбор `find_rooms`/`match_rooms`, код доступа (bcrypt-хэш, версия) и подпись cookie; коды 001, 003, 005 |
| `api.py` | Публичный интерфейс (единственная точка входа извне) |
| `tests/` | Схема и пути отказов, подбор и порядок, версия и строка журнала, изоляция тенантов, код доступа: хэш, проверка, смена гасит прежнюю подпись, короткий код |

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

## Настройки

`TenantConfig.showroom` — `platform/showroom_config.py` (§2.5): `enabled` (кабинет), `access_code_hash`
и `access_code_version` (страница по коду; ставит только команда
`python -m hospitality.tools.showroom_access_code set --tenant <slug>`), `public_enabled` (после
допсоглашения, PR г), службы `sales_category_key` и `booking_category_key`, `booking_max_guests`,
`assistant_enabled`, `contact_note`. Онбординг берёт витрину из профиля, а код всегда переносит из
прежнего конфига и задать его файлом не даёт.

## Таблицы

`showroom_scenes` — миграция 0059, RLS по канону 0002. FK в чужие модули нет.

## Зависимости

`platform` (конфиг, журнал изменений), `shared`. Модуль не импортирует `ai/`, `channels/`,
`integrations/`. Потребители: `channels/showroom` (страница посетителя), `staff_portal`
(раздел «3D-витрина»), `tools/showroom_access_code`, `tools/demo_showroom` (сид staging).

## Типовые сценарии изменения

- Новое поле сцены — в `scene.py` с умолчанием (старые сцены в БД обязаны читаться), проверка
  ссылок — в `check_references`, просмотрщик — `channels/showroom/assets/viewer.js`.
- Здание не прямоугольной формы — файл модели вместо параметров (ADR-025 «Пересмотреть»).
