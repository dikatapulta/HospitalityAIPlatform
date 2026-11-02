# integrations/pms — адаптеры источника проживаний

Реализации порта `PmsPort` (`shared/ports/pms.py`,
[spec 0058](../../../../docs/specs/0058-pms-stay-sync.md) §2.1,
[ADR-004](../../../../docs/adr/ADR-004-pms-stay-source-opera-report-snapshot.md)).
Подключает только composition root — `app.py` и `worker.py` зовут
`build_pms(settings, max_guests=…)` и кладут результат в порт (`set_pms`); `app.py`
ещё монтирует маршруты приёма выгрузок (`build_pms_upload_router`). Ядро и модули
адаптеры не импортируют (контракт 2 import-linter).

## Что умеет

Отдаёт последний принятый снимок проживаний тенанта: `fetch_snapshot(tenant_id)` —
`PmsSnapshot` (`source_id`, `taken_at` в UTC, строки `PmsStayRow`: комната, номер
брони, даты заезда и выезда по календарю отеля, число гостей или `None`) или `None`,
если снимков ещё не было. Имён гостей в снимке нет (ADR-004). Что делать со снимком —
работа сверки `modules/guests` (spec 0058 §2.2).

Второй файл того же отеля — свободные номера по ночам
([spec 0059](../../../../docs/specs/0059-stay-extension-availability.md) §2.2):
`fetch_availability(tenant_id)` — `PmsAvailability` (`taken_at` в UTC, ночи
`PmsNightAvailability` подряд по возрастанию: дата ночи и число свободных номеров,
≤ 0 — мест нет) или `None`, если файлов ещё не было. Броней, комнат и гостей в нём
нет. Со снимком не связан: один приходит без другого. Читает `guests_api.get_extension_outlook`.

| Адаптер | Файл | Откуда снимок | Для чего |
|---|---|---|---|
| `FakePmsAdapter` | `fake.py` | память процесса: «дом отеля» двигают `check_in_room` / `check_out_booking` / `move_booking` / `set_departure`, снимок выпускает `publish()`; свободные номера кладёт сразу `set_availability()` | тесты, CI, локальный uvicorn (ADR-007) |
| `OperaReportPmsAdapter` | `opera_report/` | файлы отчётов Opera, принятые маршрутами загрузки: снимок «кто в доме» и свободные номера по ночам; последний файл каждого вида — в таблице `pms_report_feeds` (миграции 0052, 0053) | staging, гостевая копия |

Правки «дома» Fake в снимок не попадают до `publish()`; каждый `publish()` — новый
`source_id`, даже если строки те же (так сверка отличает новый снимок от повтора).
У адаптера Opera так же: каждый принятый файл — новый `source_id`, кроме того же файла
байт в байт в пределах минуты (повтор задачи планировщика — `{"duplicate": true}`).
Файл свободных номеров по тому же правилу повтора заменяет прежний целиком; снимка
он не трогает, а снимок — его.

## Адаптер Opera (`opera_report/`)

| Файл | Что делает |
|---|---|
| `parse.py` | `parse_report` — файл → строки снимка по контракту spec 0058 §2.4; `parse_availability` — файл → ночи по spec 0059 §2.1 (колонки `date`, `free_rooms`, ночи подряд, 1…62); нарушение — `ReportRejectedError` (класс причины и номер строки, без содержимого) |
| `adapter.py` | `OperaReportPmsAdapter` (`fetch_snapshot`, `fetch_availability`, `check`, `accept_upload`, `accept_availability`); токен загрузки — `issue_upload_token`, `upload_token_matches` (в БД — SHA-256, ADR-008) |
| `router.py` | `POST /integrations/pms/{tenant_slug}/opera-report` и `…/opera-availability`: токен → частота → приём включён у отеля → размер → разбор; коды `ERR-PMS-001…005`; ответ второго — `{"accepted": true, "nights": N}`. Счётчик частоты и метрика у маршрутов свои |
| `models.py` | `PmsReportFeed` — строка на тенанта: хэш токена, последний снимок и последний файл свободных номеров (RLS, канон `TenantIsolationCanary`) |

Файл любого вида принимается, только если у отеля `TenantConfig.pms_source: opera_report`
(второго выключателя у свободных номеров нет, spec 0059 §2.5); иначе —
409 `ERR-PMS-005`, ничего не сохраняется: токен можно выпустить и отдать IT заранее,
а номера броней не лягут в базу до допсоглашения к договору (spec 0058 §4). Выпуск и
перевыпуск токена — `python -m hospitality.tools.pms_token <код-отеля>`; шаги для IT
отеля — [opera-report-upload.md](../../../../docs/runbooks/opera-report-upload.md), второй
отчёт — его раздел «Отчёт свободных номеров».

## Конфигурация

| Переменная | Значение |
|---|---|
| `PMS_BACKEND` | `fake` (умолчание кода) или `opera_report` (compose задаёт сам — у app и worker одинаково) |

Потолок гостей одного Stay (`max_guests`) адаптер получает от composition root —
это `guests_api.MAX_GUESTS_PER_STAY`, а модуль `guests` адаптер импортировать не вправе
(контракт 3). Сверка идёт только у тенанта с `pms_source: opera_report` — у остальных
снимок никто не читает.

## Health и метрики

`check()` — готов ли источник; оба адаптера отвечают `True` (снимок Opera — в той же
БД, её проверяет `/health/ready`). В `/health/ready` порт не входит: опоздавший
снимок — алерт `ERR-GUESTS-010` (spec 0058 §2.6), а не отказ приложения. Метрики
маршрутов — `pms_report_uploads_total{outcome}` и `pms_availability_uploads_total{outcome}`
(`shared/metrics.py`). Алерта на остановку второго файла нет (spec 0059 §6): файл старше
часа бот не читает и отвечает о продлении как v1.

## Тесты

Один контрактный набор на все адаптеры — `tests/test_pms_port.py` (R-7). Порт только
читает, поэтому у адаптера в наборе своя «выгрузка» (`_Harness.publish`); новый адаптер —
строкой в `ADAPTERS`: не прошёл контракт — дефект адаптера. Разбор обоих файлов —
`tests/test_pms_opera_parse.py`, маршруты, токен и сквозной путь до Stay —
`tests/test_pms_opera_report.py`.
