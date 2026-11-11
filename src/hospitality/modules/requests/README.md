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
| `models.py` | `RequestCategory`, `ServiceRequest` — тенантные таблицы (канон RLS Task 0009); `RequestStatus` — жизненный цикл; `ServiceRequestOrigin` — источник заявки; `CancelReason` и `CANCEL_REASON_LABELS` — причина отмены и её подписи персоналу (spec 0057) |
| `service.py` | `create_category`, `rename_category`, `create_request`, `change_request_status`, `transfer_request`, `get_request`, `list_requests`, `list_categories`, `find_open_requests_by_daily_number`, `list_open_requests_by_ids`, `list_requests_by_ids`, `list_unclaimed_requests`, `list_stalled_requests`, `list_open_requests`, `list_requests_created_since`, `list_requests_closed_since`, `anonymize_expired_request_texts`; карта переходов `STATUS_TRANSITIONS`; присвоение дневного номера; коды ошибок |
| `day_summary.py` | `day_summary(service_day, *, category_ids=None)` + схемы `RequestsDaySummary` / `DayServiceCounts` — числа заявок за сутки отеля (spec 0035 §6). Отдельным файлом, а не в `service.py`: тот и без него за границей R-3 (~400 строк) |
| `payment.py` | `mark_request_paid` — отметка «Оплачено» (spec 0038 Р-2). Отдельным файлом по той же причине, что `day_summary.py` |
| `direction.py` | `direct_request` — «Направить» невзятую заявку сотруднику (spec 0053 §4а, #856): `directed_to_*` и строка журнала `request_directed`. Отдельным файлом по той же причине |
| `staff_stats.py` | `staff_stats(*, since, until, category_ids)` + схема `StaffStatsRow` — итоги по сотруднику за период из журнала изменений (spec 0065 §2.2). Отдельным файлом по той же причине |
| `quota.py` | `check_quota` — счёт квоты бесплатного на сегодня (spec 0056 §4). Отдельным файлом по той же причине |
| `details.py` | `append_request_details` — дописать строку в подробности невзятой заявки (spec 0039 §2.5, #863). Отдельным файлом по той же причине |
| `sent.py` | `list_requests_sent_by_services` — заявки, которые службы отправили другим службам (spec 0063 §3). Отдельным файлом по той же причине |
| `reminders.py` | `record_guest_reminder`, `list_guest_reminded_requests` — гость напоминает о заявке (spec 0070 §3). Отдельным файлом по той же причине |
| `remainder.py` | `complete_with_remainder` — «Готово» с остатком: закрытие исходной и заявка на остаток одной транзакцией ([spec 0079](../../../../docs/specs/0079-partial-completion-remainder.md) §2.1, #58). Отдельным файлом по той же причине; общие куски с `service.py` — `_apply_status_change` и `_insert_request` (тело перехода и вставки в сессии вызывающего) |
| `reopen.py` | `reopen_request`, `can_reopen`, `REOPEN_WINDOW` — «Вернуть в работу» выполненную: `done → in_progress` в течение суток после закрытия ([spec 0080](../../../../docs/specs/0080-reopen-done-request.md) §2). Отдельным файлом по той же причине |
| `events.py` | `RequestCreated`, `RequestStatusChanged`, `RequestCategoryChanged` (канон событий Task 0010; передача — spec 0042), `RequestPaymentMarked` (spec 0038); `RequestInitiator` (spec 0025) |
| `schemas.py` | Pydantic-схемы границ: `*Create` на входе, `*Read` на выходе (R-6); страница списка `ServiceRequestPage`; `FloorScope` — срез лент по этажам (spec 0053 §2). `ServiceRequestRead` отдаёт `origin`, `claimed_at`, `closed_at`, `claimed_by_*`, `directed_to_*` (кому направили, spec 0053 §4а), `closed_by_display_name` (карточка заказа показывает, кто нажал каждый шаг, spec 0038 §5; до неё наружу уходил только момент закрытия), оплату — `payment_required`, `paid_at`, `paid_by_display_name` — и напоминания гостя `guest_reminder_count`, `guest_reminded_at` (spec 0070 §3), исходную заявку остатка `parent_request_id` (spec 0079), число возвратов в работу `reopen_count` (spec 0080); `RemainderOutcome` — итог «Готово» с остатком. Id пользователей закрывшего и отметившего оплату наружу не уходят |
| `router.py` | **CANONICAL ENDPOINT** (Task 0013): HTTP API `/api/v1/requests` поверх `service.py` |
| `tests/` | Жизненный цикл, публикация событий, изоляция тенантов, HTTP API, метки времени и «кто закрыл» (`test_measurability.py`), числа сводки дня (`test_day_summary.py`), возврат в очередь и передача (`test_handover.py`), граница службы — срезы лент в SQL и `ERR-REQUESTS-007` под блокировкой строки (`test_service_scope.py`), оплата при выполнении, повтор по `request_id` и `only_from` с гонкой (`test_payment.py`), счёт квоты по дню отеля (`test_quota.py`), итоги по сотруднику — возврат, передача, срок службы строки взятия, граница старшего (`test_staff_stats.py`), снимок служб создателя и выборка «Отправленных» (`test_sent.py`), счёт напоминаний гостя — гонка, закрытая, RLS — и их выборка (`test_reminders.py`), дописывание в подробности только невзятой и без обрезки (`test_details.py`), «Готово» с остатком — поля остатка, два события, журнал, отказ без остатка, повтор при коллизии номера (`test_remainder.py`), «Вернуть в работу» — снятые метки закрытия, событие и журнал, сутки, заявка формы, только `done`, устаревшее «Взять», итоги по сотруднику (`test_reopen.py`) |

## Публичный API (`api.py`)

- `create_request(ServiceRequestCreate, *, request_id=None, acting_user=None,
  stay_id=None, quota_key=None, quota_units=None) -> ServiceRequestRead` — заявка в
  статусе `new` + событие `request.created` в той же транзакции. Присваивает
  **дневной номер `#N`** (см. ниже). Требует **`origin`** — источник заявки
  (`ServiceRequestOrigin`: `guest_chat` | `staff_manual` | `api` — spec 0035 §4,
  `escalation` — бот не справился и позвал человека, spec 0039 §2.4,
  `guest_form` — гость оформил сам формой на странице: заказ в номер, spec 0038 Р-7,
  `showroom_form` — посетитель 3D-витрины отправил запрос формой, spec 0069 §2.4; вне
  доли — ни в числителе, ни в знаменателе; `feedback` — гость поставил палец вниз
  выполненной заявке, карточка в службе эскалаций, spec 0076 §4.3; как `escalation` —
  в знаменателе, не в числителе; `remainder` — остаток частично выполненной, создаёт
  только `complete_with_remainder`, spec 0079 §2.2; вне доли, как `showroom_form`):
  поле обязательное и умолчания не имеет ни в схеме, ни в БД, потому что из
  доли `guest_chat` + `guest_form` считается Exit-критерий Phase 1 (формула —
  spec 0035 §4) — путь, забывший назвать
  источник, молча подмешался бы в измеряемое число. Принимает необязательный
  `guest_language` (ISO 639-1) — язык гостя для статусных уведомлений
  (spec 0021 П-1) — и `is_urgent` (умолчание `False`) — признак срочности
  (spec 0034 §5). На срочность модуль не смотрит: он её хранит и отдаёт,
  а решают по ней слои выше — AI снимает гейт подтверждения (ADR-018),
  канал маркирует уведомление службе, а ночную доставку ветвит issue #212.
  Отдельного статуса «срочная» нет намеренно: срочность ортогональна стадии
  работы (тот же довод, что в ADR-013). `acting_user` — сотрудник, внёсший
  заявку формой кабинета: строка журнала изменений «Создана вручную» той же
  транзакцией (spec 0041) и снимок его служб (`acting_user.service_keys`) в
  `created_by_service_keys` — вкладка «Отправленные» ([spec 0063](../../../../docs/specs/0063-sent-requests-tab.md)
  §2); `None` (ИИ, HTTP API) — журнал не пишется, снимок пуст.
  `payment_required` (умолчание `False`) — оплата при выполнении (spec 0038 Р-2,
  ниже). `request_id` — id, выбранный вызывающим заранее (spec 0038 Р-3: заказ
  пишет свой id до заявки); вызов идемпотентен по нему (P-8): заявка с таким
  id есть — возвращается она, без второй заявки и второго события, в том числе
  при параллельном повторе (нарушение первичного ключа). В HTTP-схеме id нет
  намеренно (ревью PR #497, Н-5): id снаружи мог бы совпасть с заявкой другого
  тенанта, которую RLS не покажет. `stay_id` — проживание, для которого заявка
  ([spec 0054](../../../../docs/specs/0054-request-stay-link.md)): передаёт
  только вызывающий с проверенным Stay (веб-чат, заказ в номер, запрос АВР);
  тоже аргумент, а не поле HTTP-схемы, — снаружи заявку чужому Stay не приписать.
  Отдаётся в `ServiceRequestRead.stay_id`. `quota_key` + `quota_units` — заявка
  по квоте бесплатного ([spec 0056](../../../../docs/specs/0056-service-quotas.md)
  §3): только парой и 1–20 штук, иначе `ValueError` до записи; тоже аргументы,
  а не поля HTTP-схемы. Что квота у отеля есть и штук не больше остатка, проверяет
  вызывающий (AI-инструмент) — модуль метку хранит и считает. Отдаются в
  `ServiceRequestRead.quota_key` / `quota_units`.
- `check_quota(ServiceQuota, *, stay_id, guests_count, units) -> QuotaCheck` —
  счёт квоты бесплатного на сегодня (spec 0056 Р-2, Р-3, `quota.py`): `limit` —
  норма × `guests_count`; `used` — сумма `quota_units` заявок этого Stay с этим
  ключом за сегодняшний `service_day` (тот же день отеля, что у `#N`), кроме
  отменённых — открытые в счёт; `free_units = min(units, max(limit − used, 0))`.
  Правило квоты — данные тенанта (`platform.service_quotas`); что сказать гостю,
  решает вызывающий, и лог исхода — его (`service_quota_checked`).
- `change_request_status(request_id, RequestStatus, resolution_note=,
  cancel_reason=, initiator=, acting_user=, allowed_category_ids=, only_from=) ->
  ServiceRequestRead` — переход по жизненному
  циклу + событие `request.status_changed`. `resolution_note` — примечание
  персонала к закрытию (частичное выполнение / текст «Другое» у отмены, spec 0021 П-4):
  пишется только на терминальном переходе, на прочих игнорируется с
  warning-логом. `cancel_reason` (`CancelReason`, spec 0057) — причина отмены
  из списка: пишется только переходом в `cancelled`, на прочих — игнор с
  warning-логом; отмена с `initiator=guest` всегда получает `by_guest`.
  Требует причину вход персонала (кабинет), не домен — у заказа в номер и
  запроса АВР причина своя. Подписи персоналу — `CANCEL_REASON_LABELS`. `initiator` (`RequestInitiator.GUEST|STAFF|SERVICE`, spec 0025) — кто
  инициировал переход; уезжает в событие и в лог `service_request_status_changed`
  как есть, `None` — не указан (Telegram-пути персонала). `SERVICE` — HTTP API по
  сервисному токену (#237): подписчики ведут себя как со `STAFF`. `acting_user` (`ActingUser` — реэкспорт
  `platform.staff_actions.StaffActor`: user_id + display_name + login,
  spec 0033 §5, spec 0041 §2.3) — кто действует из кабинета: на переходе
  `new → in_progress` заполняет `claimed_by_*`, на терминальном — `closed_by_*`,
  и на любом переходе пишет строку журнала изменений той же транзакцией
  (взята / выполнена — с признаком «было примечание», без его текста /
  отменена / возвращена в очередь); отказ перехода строки не оставляет.
  **Возврат в очередь** `in_progress → new` (spec 0042 §2.1) обнуляет
  `claimed_at` и `claimed_by_*` разом, с личностью или без; `created_at` не
  трогается — возраст и просрочка считаются от просьбы гостя.
  `None` (Telegram-суррогат, HTTP API, отмена гостем) — имена остаются пустыми,
  журнал не пишется.
  `allowed_category_ids` — службы, над заявками которых действующий вправе
  работать (spec 0045 §5): заявка другой службы — `ERR-REQUESTS-007` (403)
  раньше проверки перехода, ничего не меняется. Проверка — под той же
  блокировкой `FOR UPDATE`, что переход: заявку, переданную за секунду до
  нажатия, прежняя служба не закроет. `None` — любые службы (ресепшен,
  менеджер, пути без личности).
  `only_from` — переход, только если статус сейчас ровно такой, иначе
  `ERR-REQUESTS-003`; сверяется под той же блокировкой (spec 0038 §4): из «гость
  отменяет невзятый заказ» и «сотрудник его берёт» выигрывает один.
  **Оплата при выполнении** (spec 0038 Р-2) — здесь, а не у вызывающих: заявку
  с `payment_required` без отметки не закрыть `done` ни из одного канала
  (`ERR-REQUESTS-006`, 409), отмену оплаченной гостем (`initiator=guest`) домен
  отвергает (`ERR-REQUESTS-008`, 409). Сотрудник оплаченную отменить может
  (возврат денег — касса отеля). Возврат в очередь и передача отметку не
  снимают: для гостя заказ остаётся «Оплачен» (spec 0038 Р-4).
  **Метки времени пишутся всегда, имена — только с личностью** (spec 0035 §3):
  `claimed_at` ставится на каждом переходе `new → in_progress`, `closed_at` — на
  каждом терминальном, из любого канала. Иначе медиана времени взятия считалась
  бы по одному кабинету и молча занижала объём работы, сделанной через Telegram
  (на пилоте оба пути живут одновременно). Отсюда предикат, который обязана
  сохранить любая правка карты переходов: `claimed_by_user_id IS NOT NULL` ⟹
  `claimed_at IS NOT NULL` (и то же у пары закрытия); обратное неверно — «взято,
  но некем» это норма данных. `closed_at` отвечает на «когда закрыли», а не
  «закрыта ли»: «заявка открыта» считается по статусу.
- `complete_with_remainder(request_id, *, resolution_note, initiator=None,
  acting_user=None, allowed_category_ids=None) -> RemainderOutcome(closed, remainder)` —
  «Готово» с остатком ([spec 0079](../../../../docs/specs/0079-partial-completion-remainder.md),
  `remainder.py`): переход в `done` с примечанием — ровно как у `change_request_status`
  (те же проверки, ошибки, журнал и событие) — и в той же транзакции заявка `new` на
  остаток: служба, комната, `stay_id`, `guest_language` и суть — исходной, `details` —
  «Остаток заявки #N от ДД.ММ. Примечание: …», `origin = remainder`, `parent_request_id` —
  id исходной; срочность, оплата, квота и адресат не наследуются. С `acting_user` — ещё
  строка журнала «создана» и снимок служб, как у формы. Пустое примечание — `ValueError`
  до записи. Отказ перехода не оставляет остатка; коллизия дневного номера — повтор
  всей транзакции. Гостю остаток не пишется: к диалогу (`request_origins`) он не привязан.
- `reopen_request(request_id, *, initiator=None, acting_user=None,
  allowed_category_ids=None) -> ServiceRequestRead` — «Вернуть в работу»
  ([spec 0080](../../../../docs/specs/0080-reopen-done-request.md) §2, `reopen.py`):
  выполненная, закрытая меньше `REOPEN_WINDOW` (24 ч) назад, становится `in_progress`.
  Снимаются `closed_at`, `closed_by_*` и `resolution_note` — следующее «Готово» пишет
  своё; пара взятия остаётся — заявка у того, кто её делал. `reopen_count` растёт на 1.
  Публикует `request.status_changed` (`done → in_progress`, `initiator` как передан);
  с `acting_user` — строка журнала `request_reopened` той же транзакцией. Отказы под
  блокировкой строки: чужая служба — `ERR-REQUESTS-007` (403) раньше прочих; не `done` —
  `ERR-REQUESTS-003` (409); `origin` `guest_form` или `showroom_form` (заказ, бронь, АВР,
  витрина — их шаги гостю ведут свои подписчики, поправки у них нет) или прошли сутки —
  `ERR-REQUESTS-009` (409). Остаток, заведённый закрытием (spec 0079), остаётся своей
  заявкой. `can_reopen(request, now=None) -> bool` — то же правило без записи: по нему
  кабинет и чат службы рисуют кнопку.
- `transfer_request(request_id, category_id, *, acting_user=None) ->
  ServiceRequestRead` — передать открытую заявку в другую службу (spec 0042
  §2.2). Взятая заодно возвращается в очередь новой службы (обнуление пары
  взятия, как у возврата). Публикует `request.category_changed`, у взятой — ещё
  и `request.status_changed` (`in_progress → new`, `initiator=staff`) той же
  транзакцией. Чужая/несуществующая служба — `ERR-REQUESTS-001` (404);
  закрытая заявка или та же служба — `ERR-REQUESTS-005` (409). С `acting_user` —
  одна строка журнала «передана» с `category_name`/`category_id` (откуда) и
  `category_name_to`/`category_id_to` (куда); id службы пишет каждая строка о
  заявке — по нему старший видит строки своих служб (spec 0045 §3).
  `allowed_category_ids` ограничивает службу,
  откуда передают (`ERR-REQUESTS-007`, как у `change_request_status`); куда —
  любая служба отеля. Передача снимает `directed_to_*`: в новой службе адресата нет.
- `direct_request(request_id, *, to_user_id, to_display_name, acting_user,
  allowed_category_ids=None) -> ServiceRequestRead` — «Направить» (spec 0053 §4а,
  `direction.py`): пара `directed_to_user_id` + снимок имени (миграция 0062) и строка
  журнала «направлена» без имени адресата. Только `new`, иначе `ERR-REQUESTS-003`
  (409); граница службы — `ERR-REQUESTS-007` под блокировкой строки. Статус не
  меняется, события нет. Кто вправе и кому можно — кабинет.
- `FloorScope(category_ids, hidden_floors, user_id)` — необязательный `floor_scope`
  у `list_open_requests`, `list_requests_created_since`, `list_requests_closed_since`,
  `list_unclaimed_requests`, `list_stalled_requests` (spec 0053 §2, #856): заявки
  служб среза из номеров на `hidden_floors` (этаж — `shared/rooms.floor_of_room_sql`)
  не видны, кроме взятых или направленных `user_id`, срочных и эскалаций; номер без
  этажа срез не трогает. В SQL до `limit`, как остальные фильтры.
- `mark_request_paid(request_id, *, acting_user, allowed_category_ids=None) ->
  ServiceRequestRead` — отметка «Оплачено» (spec 0038 Р-2, `payment.py`):
  `paid_at` + `paid_by_*`, строка журнала изменений «Оплачено»
  (`request_payment_marked`, spec 0041 §2.2) и событие `request.payment_marked`
  той же транзакцией.
  Ставит только человек (P-9): `acting_user` обязателен. Применима к взятой
  (`in_progress`) заявке с признаком и без отметки, иначе `ERR-REQUESTS-008`
  (409) — повторное нажатие не перезаписывает, кто взял деньги. Граница службы —
  как у `change_request_status` (`ERR-REQUESTS-007` под той же блокировкой
  строки; ревью PR #497, Н-4). Снять отметку нечем. Не финансовая запись: чек —
  в кассе отеля (NG-3). Кнопка — карточка заказа кабинета (spec 0038 §6 PR 6).
- `append_request_details(request_id, line) -> ServiceRequestRead | None` —
  дописать `line` новой строкой в `details` заявки в статусе `new` (`details.py`,
  spec 0039 §2.5, #863): так дедупликация эскалаций не теряет новую просьбу гостя.
  None — дописать некуда, вызывающий заводит новую заявку: заявки у тенанта нет,
  её взяли или закрыли, или строка не влезает в 4000 знаков (`REQUEST_DETAILS_MAX_CHARS`
  схемы; не обрезается). Под блокировкой строки; ни события, ни журнала изменений.
- `get_request(request_id) -> ServiceRequestRead`.
- `find_open_requests_by_daily_number(daily_number) -> list[ServiceRequestRead]`
  — незакрытые заявки тенанта с этим дневным номером (резолв команды `/done N`
  в staff-чате). Список, а не одна: номер за сутки может повториться, тогда
  вызывающая сторона просит уточнить (см. «Дневной номер»).
- `list_open_requests_by_ids(request_ids) -> list[ServiceRequestRead]` —
  незакрытые заявки тенанта среди переданных id, в порядке создания
  (spec 0025: опора снапшота «активные заявки диалога» — id передаёт канал из
  своей привязки `request_origins`; spec 0039 §2.5: опора дедупликации
  эскалаций — id из `conversation_escalations.request_id`; терминальные и
  чужие тенанту молча выпадают).
- `list_requests_by_ids(request_ids) -> list[ServiceRequestRead]` — заявки
  тенанта среди переданных id в любом статусе, в порядке создания (spec 0049
  Р-10: страница «Документы для АВР» берёт у заявки статус и `#N`; чужие
  тенанту и несуществующие молча выпадают). `ServiceRequestRead.service_day` —
  день отеля, к которому относится `#N`: снапшот для строки журнала, которую
  пишет модуль `documents`.
- `list_unclaimed_requests(created_before=, limit=, category_ids=None) ->
  list[ServiceRequestRead]` — заявки тенанта, которые никто не взял: статус
  `new` и создана раньше границы, новые сверху, не больше `limit` (spec 0028:
  опора напоминаний о висящих заявках; spec 0039 §3 — сигнал кабинета). Модуль не знает ни про сроки, ни про адресатов — порог
  берёт из конфига тенанта вызывающая сторона. Порядок «новые сверху» вместе с
  `limit` существен: срез не должен «съедаться» хвостом старых висяков.
- `list_stalled_requests(claimed_before=, limit=, category_ids=None) ->
  list[ServiceRequestRead]` — пара предыдущей для взятых и не закрытых (#120,
  spec 0028 §8): статус `in_progress` и взята (`claimed_at`, у старых строк без
  него — `created_at`) раньше границы, недавно взятые сверху.
- `record_guest_reminder(request_id) -> ServiceRequestRead | None` — гость
  напомнил об открытой заявке (spec 0070 §3): один атомарный UPDATE
  `guest_reminder_count + 1` и `guest_reminded_at` = `utc_now()` приложения (с ним
  лестница сравнивает `created_at`/`claimed_at`); `None` — заявки нет или её уже
  закрыли. Событий нет; лог `guest_reminder_recorded` (id и число, без текста).
  Кого звать, решает лестница кабинета по настройке отеля, не модуль.
- `list_guest_reminded_requests(limit=, category_ids=None, floor_scope=None) -> list[ServiceRequestRead]`
  — открытые заявки, о которых гость напоминал, свежие напоминания сверху
  (spec 0070 §5: взятую поднятую сигнал зовёт и без срока «долго в работе»).
- `list_open_requests(limit=, category_ids=None, claimed_by_user_id=None,
  room_number=None, daily_number=None) -> list[ServiceRequestRead]` — открытые
  заявки тенанта (`new` + `in_progress`), новые сверху (spec 0033 §5: лента
  очереди кабинета); `limit` — страховка от неограниченного скана.
  **Фильтры лент** (`category_ids` — и у `list_unclaimed_requests`/`list_stalled_requests`, остальные —
  у трёх лент очереди): `category_ids` — только эти службы, `None` — все,
  пустой набор — пустой ответ без запроса; `claimed_by_user_id` — только взятые
  этим сотрудником («мои»); `room_number` — комната целиком, без учёта регистра
  и пробелов по краям (нормализации при записи нет); `daily_number` — `#N`
  (сбрасывается за сутки — на длинной ленте ему отвечает несколько заявок). Все —
  в SQL до среза `limit` (spec 0045 §5, #221): в памяти после среза вкладка
  службы при длинной общей очереди молча показывала бы подмножество.
- `list_requests_created_since(created_after=, limit=, …фильтры лент) ->
  list[ServiceRequestRead]` — заявки в любом статусе с `created_at >=
  created_after`, новые сверху (spec 0033 §5: вкладка «за 7 дней», #221);
  границу (полночь отеля) считает вызывающая сторона.
- `list_requests_closed_since(closed_after=, limit=, …фильтры лент) ->
  list[ServiceRequestRead]`
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
- `list_requests_sent_by_services(service_keys, *, limit=, room_number=None,
  daily_number=None) -> list[ServiceRequestRead]` — заявки в любом статусе,
  созданные формой сотрудником одной из `service_keys` и стоящие сейчас в службе
  не из них, новые сверху (spec 0063 §3, `sent.py`): `created_by_service_keys &&
  :keys` и ключ категории не в `:keys` — оба в SQL до среза `limit`. Переданная
  обратно в свою службу заявка отсюда уходит. Пустой набор — пустой ответ без
  запроса; поиск — как у лент очереди.
- `list_requests(limit=, offset=) -> ServiceRequestPage` — страница заявок
  тенанта, новые сверху (канон пагинации Task 0013).
- `anonymize_expired_request_texts(created_before=) -> int` — обезличить
  свободный текст заявок старше границы (issue #42, spec 0032): `summary` →
  `REQUEST_TEXT_ANONYMIZED_PLACEHOLDER`, `details`/`resolution_note` → NULL,
  `stay_id` → NULL (spec 0054 §3);
  агрегаты (статус, категория, комната, номер, времена) не трогаются — отчёты
  живут. Идемпотентна (P-8): повторный вызов обновляет 0 строк. Модуль не
  знает про срок — границу (90 дней политики) считает вызывающая сторона
  (`channels/common/retention.py` из цикла воркера).
- `day_summary(service_day, *, category_ids=None) -> RequestsDaySummary` — числа
  заявок за один день отеля (spec 0035 §6, issue #300); `category_ids` — только
  эти службы, срез в каждой выборке SQL (сводка старшего, spec 0045 §3): создано (с разбивкой по **всем**
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
  знаменатель Exit-критерия Phase 1, а `guest_chat` + `guest_form` — его
  числитель (issue #313, spec 0035 §4). Среза `limit` у функции нет намеренно: у счётчика урезанная выборка
  даёт не «показали не всё», а неверное число, и молча.
  `RequestsDaySummary.has_no_requests()` — половина предиката «пустой день» (§9),
  та, что про числа этого модуля; вторую («и бот никого не звал») добавляет
  показывающая сторона, потому что эскалации считает другой владелец. Считает её
  модуль, а не каждая поверхность заново: короткой строкой пустой день
  показывают и страница кабинета, и утреннее сообщение (issue #301).
- `staff_stats(*, since, until, category_ids) -> list[StaffStatsRow]` — итоги
  каждого сотрудника, у кого в `[since, until)` есть взятие или «Готово»
  (spec 0065 §2.2, issue #704): взято, выполнено, медианы «Ждали до „Взять“» и
  «Выполняли», просрочено, возвращено в очередь. Источник — журнал ядра
  (`staff_actions.list_request_events`): возврат стирает `claimed_by_*`, и
  «взял, а потом вернул» из заявки не восстановить; заявка даёт только
  `created_at` и последнее `claimed_at`. Ожидание — от прихода в службу
  (последний возврат или передача до взятия, иначе создание); срок — службы
  строки взятия, а не текущей службы заявки. Возврат — у того, кто брал:
  следующая строка заявки после его взятия — `request_returned`; передача
  возвратом не считается. `category_ids` режет только взятия и «Готово» —
  возврат и передача нужны для отсчёта. Медианы `None` — мерить нечего,
  `overdue` `None` — у отеля выключены напоминания (канон `day_summary`).
- `list_categories() -> list[RequestCategoryRead]` — категории тенанта по `key`.
- `create_category(RequestCategoryCreate) -> RequestCategoryRead` — в Phase 0
  вызывается сидами и тестами.
- `rename_category(key, *, expected_name, new_name) -> bool` — сменить имя службы,
  только если оно сейчас `expected_name` (ручное имя не трогается, повтор — `False`);
  ключ не меняется. Вызывает демо-сид staging (#833).
- `router` — HTTP-роутер (ниже); подключает только composition root.

Все функции вызываются внутри `tenant_context(...)` (P-4) и сами управляют
транзакцией (`session_scope()` внутри). Ожидаемые ошибки — `AppError`
с кодами `ERR-REQUESTS-001…008` (каталог: `docs/runbooks/errors.md`).

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
| `POST /api/v1/requests/{id}/status` | Переход по жизненному циклу | 404 `ERR-REQUESTS-002`, 409 `ERR-REQUESTS-003`, 409 `ERR-REQUESTS-006` |

Отметки оплаты в HTTP API нет: её ставит только сотрудник из кабинета (P-9). Переход
статуса через API — `initiator=service` в событии (#237, аудит 05.08 Г-10): за токеном нет
человека, поэтому имени взявшего и закрывшего и строки журнала изменений нет, а след
«пришло через API» остаётся в outbox и в логе.

## Жизненный цикл статусов (ADR-013)

```
new ⇄ in_progress ⇄ done
  └────────┴─→ cancelled        (done, cancelled — терминальные)
```

`in_progress → new` — «вернуть в очередь» (spec 0042): сотрудник ушёл со
смены или заявку передали в другую службу.

`done → in_progress` — «вернуть в работу» (spec 0080): «Готово» нажали не на той
заявке. Единственное ребро из терминального статуса — не в `STATUS_TRANSITIONS`, а
`REOPEN_TRANSITION` рядом с ней и свой вход `reopen_request`: сутки после закрытия,
не заявка формы. Общий `change_request_status` закрытую по-прежнему не воскрешает —
устаревшая кнопка «Взять» под ней получает `ERR-REQUESTS-003`.

Статуса `assigned` больше нет (ADR-013, issue #75): персонал пилота не различал
«назначено» и «в работе». «Кто взял» появится в Phase 1 атрибутом assignee.

Недопустимый переход (в т.ч. в тот же статус) — `ERR-REQUESTS-003` (409).
Карта переходов — `STATUS_TRANSITIONS` и `REOPEN_TRANSITION` в `service.py`.

## События

- Публикует: `request.created` (`RequestCreated`: request_id, category_id,
  summary), `request.category_changed` (`RequestCategoryChanged`: request_id,
  old_category_id, new_category_id — передача в другую службу, spec 0042;
  подписчика пока нет, оповещение Telegram-чата новой службы ждёт первого
  отеля с ботом), `request.status_changed` (`RequestStatusChanged`: request_id,
  old_status, new_status, initiator — аддитивное поле spec 0025/§13.5:
  `guest`/`staff`/`service`/None, кто инициировал переход; по нему подписчики выбирают
  адресата — гостя не извещают о его собственной отмене, персонал — извещают),
  `request.payment_marked` (`RequestPaymentMarked`: request_id — сотрудник
  отметил оплату, spec 0038; подписчик — сообщение гостю, PR 5 серии).
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

## Таблицы (миграции `0006`, `0010`, `0012`, `0013`, `0018`, `0024`, `0025`, `0037`, `0048`, `0049`, `0056`, `0061`, `0073`, `0080`; RLS — копия канона `0002`)

- `request_categories` — `id`, `tenant_id` (FK+индекс), `key`
  (уникален в паре с `tenant_id`), `name`, `created_at`, `updated_at`.
- `service_requests` — `id`, `tenant_id` (FK+индекс), `category_id`
  (FK+индекс), `status` (VARCHAR, значения `RequestStatus`), `summary`,
  `details`, `room_number`, `stay_id` (UUID, NULL, индекс, без FK — Stay модуля
  guests; spec 0054 / миграция `0048`), `quota_key` (VARCHAR(63), NULL) +
  `quota_units` (SMALLINT, NULL) — заявка по квоте бесплатного, CHECK пары
  `ck_service_requests_quota_pair` и `ck_service_requests_quota_units_range`
  (1–20; spec 0056 §3 / миграция `0049`), `service_day` (DATE, NULL), `daily_number`
  (INT, NULL), `guest_language` (VARCHAR(2), NULL — ISO 639-1 язык гостя на
  момент создания, для статусных уведомлений, spec 0021 / миграция `0012`),
  `resolution_note` (VARCHAR(500), NULL — примечание персонала к закрытию,
  spec 0021 / миграция `0013`), `cancel_reason` (VARCHAR(16), NULL — значения
  `CancelReason`, CHECK `ck_service_requests_cancel_reason_only_cancelled`:
  только у `cancelled`; не ПД — ретеншн не трогает; spec 0057 / миграция
  `0050`), `claimed_by_user_id` (UUID, NULL — FK на
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
  `0025`; значения `escalation` и `guest_form` добавлены без миграции — spec 0039
  §2.4, spec 0038 Р-7), `payment_required` (BOOLEAN NOT NULL DEFAULT false —
  оплата при выполнении) + `paid_at` (TIMESTAMPTZ, NULL) + `paid_by_user_id`
  (UUID, NULL — FK на `users`, ondelete SET NULL) + `paid_by_display_name`
  (VARCHAR(255), NULL — PII сотрудника, docs/PII_REGISTRY.md); CHECK
  `ck_service_requests_paid_requires_payment` (`paid_at IS NULL OR
  payment_required`; все четыре — spec 0038 §3 / миграция `0037`),
  `created_by_service_keys` (VARCHAR(63)[] NOT NULL DEFAULT `'{}'` — службы
  создателя формой на момент создания; ключи, не ПД; `server_default` навсегда —
  откат образа; spec 0063 §2 / миграция `0056`), `guest_reminder_count` (INT NOT NULL
  DEFAULT 0, CHECK `ck_service_requests_guest_reminder_count_non_negative`) +
  `guest_reminded_at` (TIMESTAMPTZ, NULL) — сколько раз гость напомнил о заявке и
  когда последний; не ПД, `server_default` навсегда (spec 0070 §3 / миграция
  `0061`), `parent_request_id` (UUID, NULL, индекс — FK на `service_requests.id`,
  ondelete SET NULL: исходная заявка остатка; пишет только `complete_with_remainder`,
  не ПД; spec 0079 §3 / миграция `0073`), `reopen_count` (INT NOT NULL DEFAULT 0 —
  сколько раз выполненную возвращали в работу; пишет только `reopen_request`, по нему
  уведомитель гостя различает закрытия; не ПД, `server_default` навсегда; spec 0080 §2 /
  миграция `0080`), `created_at`, `updated_at`. Тройка
  `(tenant_id, service_day, daily_number)` — уникальный индекс
  `uq_service_requests_daily_number` (дневной номер, миграция `0010`).

Обе таблицы под RLS (ENABLE + FORCE + политика `tenant_isolation`);
изоляция покрыта обязательными тестами (`tests/test_tenant_isolation.py`
модуля).

## Зависимости

Внутренние: kernel — `hospitality.shared` (db, tenancy, events, errors,
logging), `hospitality.platform.auth` (аутентификация роутера, Task 0013) и
`hospitality.platform.config` (`load_tenant_config` — часовой пояс отеля для
дневного номера), `hospitality.platform.service_quotas` (правило квоты
бесплатного для `check_quota`, spec 0056), `hospitality.platform.staff_actions`
(журнал изменений кабинета, spec 0041; его строки — источник `staff_stats`, spec 0065).
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
  вместе (возврат заявки в `new` — spec 0042 — обнуляет и время, и имя, иначе
  «взята в 06:10» висит у заявки, которую никто не берёт). И строку журнала:
  у каждой цели перехода — своё действие в `_STATUS_ACTIONS` (тест
  `test_every_transition_with_actor_writes_one_journal_row`).
