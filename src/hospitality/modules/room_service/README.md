# room_service — еда и магазин в номер

## Назначение

Room service отеля ([spec 0038](../../../../docs/specs/0038-room-service.md), issue #418):
два каталога — «Ресторан» (служба `fnb`) и «Магазин» (служба `supply`), разделы и товары,
которые ведут сами службы, и переключатель «принимаем заказы». Заказ гостя (PR 5 серии)
уходит заявкой в службу каталога; модуль владеет каталогами, настройками и заказами, а
жизненный цикл заказа — это цикл заявки (Р-1). Зависимость односторонняя:
`room_service` → `requests.api`; в PR 3 модуль `requests` ещё не импортирует.

Каталог открыт, только пока его открывает конфиг тенанта: `TenantConfig.room_service`
включён и в нём есть этот каталог (правило — `TenantConfig.room_service_catalog`). Иначе
любой вызов отвечает `ERR-ROOMSERVICE-002`. Кто вправе править каталог
(`can_edit_catalog`), решает кабинет (PR 4): домен держит данные, а не роли.

## Состав

| Файл | Что даёт |
| --- | --- |
| `models.py` | `RoomServiceSection`, `RoomServiceItem`, `RoomServiceSettings` (тенантные, RLS-канон, миграция 0041); `RoomServiceCatalog` (`restaurant`, `shop`) |
| `service.py` | Чтение и правка каталога, «принимаем»; коды ошибок |
| `schemas.py` | `SectionWrite`, `ItemWrite` на входе; `SectionRead`, `ItemRead`, `SectionWithItems`, `CatalogRead`, `CatalogSettingsRead` наружу; `MoveDirection`; пределы названия и цены |
| `api.py` | Публичный интерфейс (единственная точка входа извне) |
| `tests/` | Порядок и перестановка (в т. ч. при равных позициях), вид гостя (скрытое, «Закончилось», пустой раздел), «не принимаем» без строки, смена раздела и запрет чужого каталога, закрытый каталог, изоляция тенантов, пределы цены и названия |

## Публичный API

Каждая функция — одна транзакция внутри `tenant_context`. Нет раздела или товара (в том
числе чужого тенанта) — `ERR-ROOMSERVICE-001` (404); каталог закрыт — `ERR-ROOMSERVICE-002`
(404).

- `get_catalog(catalog, *, include_hidden=False) -> CatalogRead` — разделы и товары по
  порядку. По умолчанию — вид гостя: без скрытых товаров и без разделов, где видимых нет;
  «Закончилось» (`is_available=False`) гость видит. `include_hidden=True` — вид панели.
- `create_section(catalog, SectionWrite)`, `rename_section(section_id, SectionWrite)` —
  раздел в конец каталога и его переименование.
- `create_item(ItemWrite)`, `update_item(item_id, ItemWrite)` — товар в конец раздела и
  сохранение формы целиком; смена раздела ставит товар в конец нового, раздел другого
  каталога — 001. Скрытие — не поле формы: сохранение не возвращает скрытый товар гостю.
- `set_available(item_id, available)` — «В наличии / Закончилось»;
  `set_hidden(item_id, hidden)` — «Скрыть из меню / Вернуть», повтор не сдвигает момент.
- `move_section(section_id, MoveDirection)`, `move_item(item_id, MoveDirection)` — на одну
  строку выше или ниже (у края — без изменений), под `FOR UPDATE` строк каталога или
  раздела; позиции перенумеровываются 0…n-1, поэтому равные позиции после гонки двух
  «Добавить» не мешают.
- `get_settings(catalog)`, `set_accepting(catalog, accepting) -> CatalogSettingsRead` —
  «принимаем» из строки БД (нет строки — не принимаем) плюс служба, сбор и заметка о
  доставке из конфига.

Не удаляется ничего: заказы ссылаются на товар, а пустой раздел гость не видит.

## Конфигурация

`TenantConfig.room_service` (`RoomServiceConfig`, `platform/config.py`): `enabled`,
`currency` (`KZT`), `languages` (`en` первый), `catalogs` — у каждого `category_key`,
`service_charge_percent` (0–30, до сотых), `delivery_note`. Источник — файл профиля отеля
(`tools/onboard_tenant`, служба каталога обязана быть категорией профиля); профиль без
ключа прежнее значение не стирает. Тексты отеля — `LocalizedText` (kernel): `en`
обязателен, `ru` и `kk` — по желанию, перевода нет — английский.

## События

Не публикует и не потребляет: у правки каталога подписчиков нет. События заказов — PR 5.

## Таблицы

Миграция 0041, RLS-канон 0002; FK в чужие модули нет.

- `room_service_sections` — `catalog`, `position`, `names` (JSONB).
- `room_service_items` — `section_id` (FK на раздел, `RESTRICT`), `position`, `names`,
  `descriptions`, `price_minor` BIGINT (тиын, CHECK > 0, в схеме — целые тенге до
  1 000 000 ₸), `is_available`, `hidden_at`.
- `room_service_settings` — PK `(tenant_id, catalog)`, `accepting_orders` DEFAULT false.

ПД нет: названия и цены — данные отеля.

## Зависимости

Внутренние: `hospitality.shared` (Base/UTCDateTime/utc_now, session_scope, AppError,
логирование, tenancy), `hospitality.platform.config` (`load_tenant_config`,
`LocalizedText`, `RoomServiceCatalogConfig`). Внешних сверх общих нет.

## Типовые сценарии изменения

- Третий каталог → значение `RoomServiceCatalog` и ключ `RoomServiceCatalogKey` в
  `platform/config.py` — оба места разом (тест сверяет).
- Новый язык текстов → поле `LocalizedText` и `LOCALIZED_TEXT_LANGUAGES` в kernel.
- Фото товара (PR 7) → `photo_file_id` миграцией, загрузка через `platform/files`.
