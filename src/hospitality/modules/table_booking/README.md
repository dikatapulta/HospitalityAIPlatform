# table_booking — бронь столика в ресторанах отеля

## Назначение

Бронь столика ([spec 0064](../../../../docs/specs/0064-table-booking.md), issue #702): гость
отправляет запрос из веб-чата — ресторан, дата, время, гости, имя и пожелания, — а ресторан
отвечает в кабинете «Подтвердить», «Предложить другое время» или «Мест нет». Плана столов
нет: книгу броней ведёт ресторан, поэтому до ответа это запрос, а не бронь (§1, §2.2).

Запрос живёт заявкой в службе ресторана (§2.1): номер `#N`, очередь, сигнал, пуш и лестница
невзятой заявки — у заявки. Модуль владеет бронью — ресторан, час, гости, ответ, отмена, — а
стадия выводится (`BookingRead.stage`). Зависимость односторонняя: `table_booking` →
`requests.api` (и `guests.api` — активно ли проживание и когда выезд). Модель брони не
создаёт и не видит: заявки нет в `request_origins`.

Открыта, только пока её открывает конфиг тенанта: `TenantConfig.table_booking` включён и в
нём есть ресторан (`TableBookingConfig.is_open`). Иначе запрос и варианты формы — 001. Ответ
ресторана от флага не зависит (ревью PR #711, Н-3).

## Состав

| Файл | Что даёт |
| --- | --- |
| `models.py` | `TableBooking` (миграция 0057) — тенантная, RLS-канон; `BookingAnswer` (`confirmed`, `other_time`, `no_seats`) |
| `schemas.py` | `BookingCreate` → `BookingRead`, `BookingPlaced`, `BookingStage`, `StaffBookingRead` (кабинет); форма — `BookingOptions`, `RestaurantOption`, `BookingDay`. Шаг и запас 30 мин, окно 14 дней, лимиты |
| `slots.py` | Часы и слоты по поясу отеля, проверка часа запроса и «Другого времени»; чтение конфига; коды 001–002 |
| `bookings.py` | Варианты формы (`get_options`) и «Отправить запрос» (`request_booking`): проверки, лимиты, строка брони, заявка; код 003 |
| `tracking.py` | Бронь после запроса: чтение, стадия, отмена гостем, заявка «Гость отменил бронь», ретеншн; коды 004, 006 |
| `answers.py` | Ответ ресторана (`answer_booking`) и «Закрыть запрос» после часа брони (`close_unanswered`), строка журнала «Ответ на бронь столика»; код 005 |
| `staff_view.py` | Бронь для кабинета: карточка очереди (`bookings_for_requests` → `StaffBookingRead`) и метка уведомления (`booking_request_ids`) |
| `api.py` | Публичный интерфейс (единственная точка входа извне) |
| `tests/` | Слоты и окно дней, запрос и заявка в службе ресторана, повтор ключа и сбой с повтором, отказы 001–002, лимиты, ретеншн, изоляция; каждый ответ из `new` и `in_progress`, повтор и иной ответ, чужая служба, «Другое время», ответ после часа и без флага; отмены до и после «Подтвердить», сбой между отметкой и заявкой отмены, «не ответил»; карточка кабинета и слоты «Другого времени», «Закрыть запрос», строка журнала, повтор ответа после сбоя (Н-1) и два одинаковых ответа разом |

## Публичный API

Каждая функция вызывается внутри `tenant_context`; проживание и идентичность гостя приходят
от канала (из сессии).

- `get_options(stay_id) -> BookingOptions` — «сегодня» отеля и рестораны по порядку конфига:
  название, заметка, `max_party`, дни со слотами «ЧЧ:ММ». Дни — от сегодня отеля до дня
  выезда включительно, не дальше 14 дней; слоты — `first_seating + k × 30 мин` до
  `last_seating`, сегодня — не раньше «сейчас + 30 мин»; день без слотов не попадает.
- `request_booking(stay_id, guest_identity_id, BookingCreate) -> BookingPlaced` — по порядку:
  повтор ключа (`client_key`) у проживания — та же бронь (`duplicate=True`), и заявка
  доводится, если в прошлый раз не создалась (Р-3); проживание (004); бронь выключена или
  ресторана нет (001); гостей больше `max_party`, день или слот вне правил (002); лимит (003:
  у проживания ≤ 3 без ответа и ≤ 10 за сутки отеля, отменённые в счёт — ревью PR #711,
  Н-6). Затем строка брони с `request_id = uuid4()` и `requests.create_request(request_id=…,
  stay_id=…)`: заявка в службе ресторана, `origin=guest_form`, суть — ресторан, дата, время
  и гости по-русски, `details` — «На имя» и «Пожелания» (номер карты маскируется).
- `list_bookings_for_stay(stay_id)` — «Ваши брони»: последние 20 запросов проживания, новые
  первыми; `get_booking_for_stay(booking_id, stay_id)` — бронь этого проживания, иначе 004.
  Обе доводят недоведённую заявку «Гость отменил бронь» (Н-4).
- `get_booking_by_request(request_id)`, `booking_guest_identity(request_id)` — подписчику
  сообщений гостю; None — заявка не бронь (и заявка «Гость отменил бронь» — не бронь).
- `cancel_by_guest(booking_id, stay_id)` — до часа брони: без ответа — заявка `cancelled`
  (`initiator=guest`), после «Подтвердить» — новая заявка «Гость отменил бронь: … — уберите
  из книги броней» в службе, где сейчас заявка брони; её id пишется вместе с отметкой
  отмены. Иначе (час прошёл, уже отменена, ответ не «Подтвердить», заявку закрыли) — 006.
  Отметка — под `FOR UPDATE` строки брони: с ответом ресторана выигрывает один.
- `answer_booking(request_id, answer, proposed_time=None, *, acting_user,
  allowed_category_ids=None)` — ответ под `FOR UPDATE` брони, затем заявка: невзятая
  берётся на того же сотрудника, потом `done`; отказ перехода снимает ответ. `proposed_time`
  — час «Другого времени» по часам отеля того же дня (в спеке — `proposed_at`; день знает
  бронь). Повтор того же ответа — без изменений; иной ответ, отменённая гостем, час брони
  прошёл (Н-2) или заявка закрыта — 005; чужая служба — ERR-REQUESTS-007 (403) до записи;
  «Другое время» без часа, вне слотов, ближе «сейчас + 30 мин» или без ресторана в конфиге
  — 002. Повтор того же ответа при открытой заявке её закрывает (ревью PR #716, Н-1): прошлый
  вызов записал ответ и упал до переходов. Заявку закрыл соседний вызов с тем же ответом
  (`done`) — ответ остаётся. Закрыл заявку этот вызов — строка журнала
  `table_booking_answered` (spec 0041 §2.2) своей транзакцией после переходов: снятый
  ответ в журнал не попадает.
- `close_unanswered(request_id, *, acting_user, allowed_category_ids=None)` — «Закрыть
  запрос»: час брони прошёл, ответа и отмены гостем нет, заявка открыта — заявка `done`
  теми же двумя переходами, гостю подписчик пишет «не ответил»; иначе 005, чужая служба —
  403. Без него такая заявка висела бы в очереди: ответить нельзя, «Готово» у брони нет.
- `bookings_for_requests(requests)` — брони уже прочитанных заявок для очереди кабинета,
  одним запросом; заявка «Гость отменил бронь» — не бронь. `StaffBookingRead` — бронь и
  кто ответил, «5 ноября», `can_answer`, `can_close_unanswered` и слоты «Другого
  времени» (тот же день, без запрошенного часа, не ближе «сейчас + 30 мин»; ресторана в
  конфиге нет — пусто). `booking_request_ids(ids)` — метка «Запрос на столик #N»
  сигнала кабинета.
- `delete_abandoned_bookings(created_before)` — брони без заявки старше суток
  (`ABANDONED_AFTER`); `detach_expired_bookings(created_before)` — обнуляет `stay_id` и
  `guest_identity_id`, ресторан, час и гости остаются. Зовёт ретеншн
  (`channels/common/retention.py`, шаг 11).

Стадия (`BookingStage`, §2.2): отмена гостем — `cancelled`; есть ответ — он
(`confirmed`, `other_time`, `no_seats`); заявка открыта и час не прошёл — `waiting`; иначе —
`no_answer` («Ресторан не ответил вовремя»), фоновой задачи нет. «Забронировано» — только
`confirmed`. `cancellable` — до часа брони при `waiting` или `confirmed`.

## Конфигурация

`TenantConfig.table_booking` (`TableBookingConfig`, `platform/table_booking_config.py`):
`enabled`, `restaurants` — до 10, ключи уникальны; у ресторана `key`, `names` и `note`
(`LocalizedText`, `en` обязателен), `category_key` — служба, что отвечает,
`first_seating` / `last_seating` — местное время, тот же день, `max_party` 1–30 (умолчание
8). Источник — файл профиля отеля (`tools/onboard_tenant`: служба ресторана обязана быть
категорией профиля); профиль без ключа прежнее значение не стирает. Шаг и запас 30 мин,
окно 14 дней — константы модуля.

## События

Не публикует и не потребляет. Шаги брони — события заявки (`request.created`,
`request.status_changed`); сообщения гостю по ним шлёт подписчик канала
(`channels/web/table/messages.py`), а не модуль.

## Таблицы

Миграция 0057, RLS-канон 0002; FK в чужие модули нет, в ядро — `answered_by_user_id` →
`users` ON DELETE SET NULL.

- `table_bookings` — `request_id` (UNIQUE), `request_created`, `stay_id` и
  `guest_identity_id` (nullable — ретеншн), `client_key` (UNIQUE `(tenant_id, stay_id,
  client_key)`), `restaurant_key` и снимок `restaurant_names` (JSONB), `booking_at` (UTC),
  `party_size` (CHECK 1–30), `guest_language`, `answer` (NULL — ждём), `proposed_at` (CHECK:
  задан тогда и только тогда, когда `other_time`), `answered_at`, `answered_by_user_id`,
  `answered_by_display_name`, `cancelled_by_guest_at`, `cancel_request_id` (UNIQUE) и
  `cancel_request_created` — заявка «Гость отменил бронь» (Р-3, Н-4; отметки нет в §3 спеки,
  она — копия `request_created`).

ПД — строки [PII_REGISTRY](../../../../docs/PII_REGISTRY.md): связь брони с проживанием
(срок — как у текста заявки) и снимок имени ответившего сотрудника.

## Зависимости

Внутренние: `hospitality.shared` (Base/UTCDateTime/utc_now, session_scope, AppError,
логирование, tenancy, метрика `table_bookings_total`, маскирование карт),
`hospitality.platform.config` (`load_tenant_config`, `LocalizedText`),
`hospitality.platform.staff_actions` (журнал изменений),
`hospitality.platform.table_booking_config`, `modules/requests` и `modules/guests` — через их
`api.py`. Внешних сверх общих нет.

## Метрики и логи

`table_bookings_total{tenant_id, outcome}`: `requested`, `confirmed`, `other_time`, `no_seats`,
`cancelled_by_guest`. Логи `table_booking_requested`, `table_booking_limited`,
`table_booking_answered`, `table_booking_closed_unanswered`, `table_booking_cancelled_by_guest`,
`table_booking_cancel_request_created` — с id брони и заявки, без имени и пожеланий.

## Типовые сценарии изменения

- Часы по дням недели или через полночь → поля `RestaurantConfig` и `slots.slot_times`
  (§9 спеки — сейчас не делаем).
- Новый ответ ресторана → значение `BookingAnswer`, стадия `BookingStage`, текст в
  `channels/web/table/texts.py` (`MESSAGES`, `stage_<ключ>`) и CHECK `proposed_at`, если
  ответ несёт час.
- Новое действие карточки кабинета → функция здесь (правило — в модуле, `StaffBookingRead`
  несёт его `can_*`), маршрут в `staff_portal/booking_api.py`, кнопка в
  `_booking_card.html`.
