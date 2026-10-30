# room_service — еда и магазин в номер

## Назначение

Room service отеля ([spec 0038](../../../../docs/specs/0038-room-service.md), issue #418):
два каталога — «Ресторан» (служба `fnb`) и «Магазин» (служба `supply`), разделы и товары,
которые ведут сами службы, и переключатель «принимаем заказы». Заказ гостя уходит
заявкой в службу каталога; модуль владеет каталогами, настройками и заказами, а
жизненный цикл заказа — это цикл заявки (Р-1): номер `#N`, комната, пожелания, шаги и
их время — у заявки, стадия выводится из неё (`order_stage`). Зависимость
односторонняя: `room_service` → `requests.api` (и `guests.api` — активно ли проживание).

Каталог открыт, только пока его открывает конфиг тенанта: `TenantConfig.room_service`
включён и в нём есть этот каталог (правило — `TenantConfig.room_service_catalog`). Иначе
любой вызов отвечает `ERR-ROOMSERVICE-002`. Кто вправе править каталог, решает
кабинет (`staff_portal/catalog.can_edit_catalog`, панель «Товары и меню», PR 4):
домен держит данные, а не роли.

## Состав

| Файл | Что даёт |
| --- | --- |
| `models.py` | `RoomServiceSection`, `RoomServiceItem`, `RoomServiceSettings` (миграция 0041), `RoomServiceOrder`, `RoomServiceOrderLine` (миграция 0044) — тенантные, RLS-канон; `RoomServiceCatalog` (`restaurant`, `shop`), `OrderCancelReason` |
| `service.py` | Чтение и правка каталога, «принимаем»; коды 001–002 |
| `orders.py` | Итог корзины (`quote`) и «Оформить» (`place_order`): проверки, строка заказа, заявка; коды 003–006 |
| `order_tracking.py` | Заказ после «Оформить»: чтение для гостя и кабинета, стадия, шаги сотрудника («Оплачено», отмена с причиной), отмена гостем, ретеншн; коды 007–009 |
| `photos.py` | Фото товара (PR 7): поставить, заменить, убрать, выдать — через узел файлов ядра `platform/files.py` (spec 0047, назначение `menu_photo`) |
| `schemas.py` | Каталог: `SectionWrite`, `ItemWrite` → `SectionRead`, `ItemRead`, `CatalogRead`, `CatalogSettingsRead`. Заказ: `CartLine`, `QuoteRequest`, `OrderPlace` → `Quote`, `OrderRead`, `OrderPlaced`, `OrderStage`. Пределы; деньги — `service_charge_minor`, `format_amount`, `format_percent` |
| `api.py` | Публичный интерфейс (единственная точка входа извне) |
| `tests/` | Каталог: порядок и перестановка, вид гостя, «не принимаем», закрытый каталог, изоляция, пределы. Фото: замена удаляет прежний файл и не трогает соседа, «Убрать», скрытый товар гостю не виден, чужой тенант, закрытый каталог и негодный файл ничего не оставляют, упавшая ссылка удаляет новый файл, две замены разом. Заказ: сбор и округление, заявка с оплатой, повтор ключа и сбой с повтором, отказы 001/003/004/005, лимиты, отмены гостем и сотрудником, «Оплачено» и 009, откат причины проигравшей отмены, заказы по заявкам очереди, стадия, чужое проживание и тенант, ретеншн |

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
  «Добавить» не мешают. Порядок сдвига — по позициям, перечитанным после блокировки:
  две одновременные перестановки обе доходят; товар, который в ту же секунду
  перенесли в другой раздел, — 001, а не 500 (ревью PR #508, П-1 и П-2).
- `catalog_of_section(section_id)`, `catalog_of_item(item_id) -> RoomServiceCatalog` —
  каталог раздела или товара, чтобы кабинет проверил право до правки. Каталог раздела
  не меняется, товар переезжает только внутри своего каталога — проверка до
  транзакции действия гонки не открывает. Нет такого — 001.
- `get_settings(catalog)`, `set_accepting(catalog, accepting) -> CatalogSettingsRead` —
  «принимаем» из строки БД (нет строки — не принимаем) плюс служба, сбор и заметка о
  доставке из конфига.

Не удаляется ничего: заказы ссылаются на товар, а пустой раздел гость не видит.

**Фото** (PR 7, `photos.py`). Файл принимает и хранит узел ядра `platform/files.py`
(spec 0047, назначение `menu_photo`): проверка в песочнице, WebP не больше 960 px
без метаданных, цвета — в sRGB, шифрование. Модуль держит id строки
`uploaded_files` в `photo_file_id`; негодный файл — коды узла `ERR-FILES-001…003`.

- `set_item_photo(item_id, data, original_name, uploaded_by_user_id) -> ItemRead` —
  поставить или заменить. Товар и открытый каталог проверяются до песочницы (001,
  002 — файл не принимается). Затем файл в узел → ссылка под `FOR UPDATE` товара →
  прежний файл удаляется после коммита. Упала запись ссылки — новый файл удаляется;
  не удалился прежний — WARNING `room_service_photo_cleanup_failed`, строка
  `uploaded_files` без ссылки остаётся (ретеншн `menu_photo` не трогает).
- `remove_item_photo(item_id) -> ItemRead` — «Убрать фото»; фото нет — без изменений.
- `catalog_of_photo(file_id)` — каталог товара с этим фото, чтобы кабинет проверил
  право до выдачи; `read_item_photo(file_id, *, include_hidden=False) -> StoredFile` —
  файл для выдачи (`platform/files.file_response`). Вид гостя — без скрытых товаров.
  Не фото товара тенанта — 001, каталог закрыт — 002, сбой хранилища — `ERR-FILES-005`
  или `-006`.

Адрес картинки — id файла: замена меняет адрес, и кэш браузера (`private,
max-age=3600` узла) старого фото не покажет.

**Заказы** (PR 5, `orders.py`, `order_tracking.py`). Каждая функция вызывается внутри
`tenant_context`; проживание и идентичность гостя приходят от канала (из сессии).

- `quote(QuoteRequest) -> Quote` — итог по ценам сервера: сумма, сбор каталога (сумма ×
  процент / 100 до целого тенге, половина вверх), итог (Р-5). Товар чужого каталога или
  несуществующий — 001, скрытый или «Закончилось» — 004.
- `place_order(OrderPlace) -> OrderPlaced` — по порядку: каталог закрыт (002), не
  принимает (003), товар (001/004), итог разошёлся с `expected_total_minor` (005), лимит
  проживания (006: открытых ≥ 3 или за сутки отеля ≥ 10 — сутки по поясу отеля, как у
  дневных номеров). Затем строка заказа с `request_id = uuid4()` и
  `requests.create_request(request_id=…)`: заявка в службе каталога, `origin=guest_form`,
  `payment_required`, суть — состав и итог по-русски, `details` — пожелания (номер карты
  маскируется). Повтор тем же `client_order_key` у проживания — тот же заказ
  (`duplicate=True`) и, если заявка в прошлый раз не создалась, она создаётся (Р-3).
- `get_order_for_stay(order_id, stay_id)` — заказ этого проживания, иначе 008 (и заказ
  без заявки — тоже 008: его не видит никто). `list_active_orders_for_stay(stay_id)` —
  «Отправлен», «Идём за оплатой», «Принят» (оплачен), новые первыми.
- `get_order_by_request(request_id)`, `order_guest_identity(request_id)` — подписчику
  сообщений гостю и кабинету; None — заявка не заказ.
- `orders_for_requests(requests)` — заказы уже прочитанных заявок (очередь и сигнал
  кабинета), словарь по `request_id`, двумя запросами; не заказ — ключа нет.
- `cancel_order_by_guest(order_id, stay_id)` — до оплаты («Отправлен», «Идём за
  оплатой»), иначе 007; причина `by_guest`. Заказ принят только после оплаты (решение
  основателя 29.10): оплаченный гостю не отменит домен заявок (ERR-REQUESTS-008) под
  блокировкой строки — «гость отменяет / сотрудник отмечает оплату» выигрывает один.
- `cancel_order_by_staff(order_id, reason, acting_user, allowed_category_ids)` — причина,
  затем отмена заявки; граница службы — у перехода заявки (ERR-REQUESTS-007), отказ
  возвращает прежнюю причину. «Отказался платить» у оплаченного — 009: отметку не
  снять, и гость прочёл бы «оплата не состоялась» рядом с «деньги вернут». Откат
  причины проигравшей отмены (гость и сотрудник в одну секунду) — только если в строке
  всё ещё своя: чужую причину победителя он не стирает. Метрика `cancelled_by_staff`.
- `mark_order_paid(order_id, acting_user, allowed_category_ids)` — «Оплачено» из
  кабинета: `requests.mark_request_paid` (граница службы 007, отказы ERR-REQUESTS-008),
  метрика `paid`. HTTP-действия обоих — `staff_portal/order_api.py` (PR 6).
- `order_stage(ServiceRequestRead) -> OrderStage` — Р-4: `cancelled`, `done`, отметка
  оплаты (`paid` — «Принят»), `in_progress` (`coming` — «Идём за оплатой», ещё не
  принят), иначе `sent`.
- `delete_abandoned_orders(created_before)` — заказы без заявки старше суток
  (`ABANDONED_AFTER`); `detach_expired_orders(created_before)` — через 90 дней обнуляет
  `stay_id` и `guest_identity_id`, состав и суммы остаются. Зовёт ретеншн
  (`channels/common/retention.py`).

## Конфигурация

`TenantConfig.room_service` (`RoomServiceConfig`, `platform/config.py`): `enabled`,
`currency` (`KZT`), `languages` (`en` первый), `catalogs` — у каждого `category_key`,
`service_charge_percent` (0–30, до сотых), `delivery_note`. Источник — файл профиля отеля
(`tools/onboard_tenant`, служба каталога обязана быть категорией профиля); профиль без
ключа прежнее значение не стирает. Тексты отеля — `LocalizedText` (kernel): `en`
обязателен, `ru` и `kk` — по желанию, перевода нет — английский.

## События

Не публикует и не потребляет. Шаги заказа — события заявки (`request.created`,
`request.status_changed`, `request.payment_marked`); сообщения гостю по ним шлёт
подписчик канала (`channels/web/menu/messages.py`), а не модуль.

## Таблицы

Миграция 0041, RLS-канон 0002; FK в чужие модули нет.

- `room_service_sections` — `catalog`, `position`, `names` (JSONB).
- `room_service_items` — `section_id` (FK на раздел, `RESTRICT`), `position`, `names`,
  `descriptions`, `price_minor` BIGINT (тиын, CHECK > 0, в схеме — целые тенге до
  1 000 000 ₸), `is_available`, `hidden_at`.
- `room_service_settings` — PK `(tenant_id, catalog)`, `accepting_orders` DEFAULT false.

Миграция 0046: `room_service_items.photo_file_id` — FK на `uploaded_files.id`
(таблица ядра) ON DELETE SET NULL, UNIQUE: файл — у одного товара.

Миграция 0044:

- `room_service_orders` — `catalog`, `request_id` (UNIQUE), `request_created`,
  `stay_id` и `guest_identity_id` (nullable — ретеншн), `client_order_key` (UNIQUE
  `(tenant_id, stay_id, client_order_key)`), `guest_language`, `currency`, суммы
  BIGINT (`subtotal_minor`, `service_charge_minor`, `total_minor`),
  `service_charge_percent` NUMERIC(5,2), `cancel_reason`.
- `room_service_order_lines` — `order_id` (CASCADE), `item_id` (RESTRICT), `position`,
  снимок `names` и `unit_price_minor`, `quantity` 1–20 (CHECK), `line_total_minor`.

Каталог ПД не содержит. Заказ — ПД гостя через `stay_id` (что купил гость в номере):
строка [PII_REGISTRY](../../../../docs/PII_REGISTRY.md), срок — 90 дней, как у текста
заявки.

## Зависимости

Внутренние: `hospitality.shared` (Base/UTCDateTime/utc_now, session_scope, AppError,
логирование, tenancy, метрика `room_service_orders_total`, маскирование карт),
`hospitality.platform.config` (`load_tenant_config`, `LocalizedText`,
`RoomServiceCatalogConfig`), `hospitality.platform.files` (фото товара),
`modules/requests` и `modules/guests` — через их `api.py`.
Внешних сверх общих нет.

## Типовые сценарии изменения

- Третий каталог → значение `RoomServiceCatalog` и ключ `RoomServiceCatalogKey` в
  `platform/config.py` — оба места разом (тест сверяет).
- Новый язык текстов → поле `LocalizedText` и `LOCALIZED_TEXT_LANGUAGES` в kernel.
- Второй снимок товара или галерея → таблица фото модуля вместо колонки
  `photo_file_id`; приём и выдача — тем же `platform/files`, правило «замена
  удаляет прежний файл» — в `photos.py`.
- Новая причина отмены → значение `OrderCancelReason` и её текст гостю в
  `channels/web/menu/texts.py` (`reason_<ключ>`), иначе гость прочтёт «Другое».
