# integrations/pms — адаптеры источника проживаний

Реализации порта `PmsPort` (`shared/ports/pms.py`,
[spec 0058](../../../../docs/specs/0058-pms-stay-sync.md) §2.1,
[ADR-004](../../../../docs/adr/ADR-004-pms-stay-source-opera-report-snapshot.md)).
Подключает только composition root — `app.py` и `worker.py` зовут
`build_pms(settings, max_guests=…)` и кладут результат в порт (`set_pms`); `app.py`
ещё монтирует маршрут приёма выгрузки (`build_pms_upload_router`). Ядро и модули
адаптеры не импортируют (контракт 2 import-linter).

## Что умеет

Отдаёт последний принятый снимок проживаний тенанта: `fetch_snapshot(tenant_id)` —
`PmsSnapshot` (`source_id`, `taken_at` в UTC, строки `PmsStayRow`: комната, номер
брони, даты заезда и выезда по календарю отеля, число гостей или `None`) или `None`,
если снимков ещё не было. Имён гостей в снимке нет (ADR-004). Что делать со снимком —
работа сверки `modules/guests` (spec 0058 §2.2).

| Адаптер | Файл | Откуда снимок | Для чего |
|---|---|---|---|
| `FakePmsAdapter` | `fake.py` | память процесса: «дом отеля» двигают `check_in_room` / `check_out_booking` / `move_booking` / `set_departure`, снимок выпускает `publish()` | тесты, CI, локальный uvicorn (ADR-007) |
| `OperaReportPmsAdapter` | `opera_report/` | файл отчёта Opera, принятый маршрутом загрузки; последний — в таблице `pms_report_feeds` (миграция 0052) | staging, гостевая копия |

Правки «дома» Fake в снимок не попадают до `publish()`; каждый `publish()` — новый
`source_id`, даже если строки те же (так сверка отличает новый снимок от повтора).
У адаптера Opera так же: каждый принятый файл — новый `source_id`, кроме того же файла
байт в байт в пределах минуты (повтор задачи планировщика — `{"duplicate": true}`).

## Адаптер Opera (`opera_report/`)

| Файл | Что делает |
|---|---|
| `parse.py` | Файл → строки снимка по контракту spec 0058 §2.4; нарушение — `ReportRejectedError` (класс причины и номер строки, без содержимого) |
| `adapter.py` | `OperaReportPmsAdapter` (`fetch_snapshot`, `check`, `accept_upload`); токен загрузки — `issue_upload_token`, `upload_token_matches` (в БД — SHA-256, ADR-008) |
| `router.py` | `POST /integrations/pms/{tenant_slug}/opera-report`: токен → частота → приём включён у отеля → размер → разбор; коды `ERR-PMS-001…005` |
| `models.py` | `PmsReportFeed` — строка на тенанта: хэш токена и последний снимок (RLS, канон `TenantIsolationCanary`) |

Файл принимается, только если у отеля `TenantConfig.pms_source: opera_report`; иначе —
409 `ERR-PMS-005`, ничего не сохраняется: токен можно выпустить и отдать IT заранее,
а номера броней не лягут в базу до допсоглашения к договору (spec 0058 §4). Выпуск и
перевыпуск токена — `python -m hospitality.tools.pms_token <код-отеля>`; шаги для IT
отеля — [opera-report-upload.md](../../../../docs/runbooks/opera-report-upload.md).

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
снимок — алерт `ERR-GUESTS-010` (spec 0058 §2.6), а не отказ приложения. Метрика
маршрута — `pms_report_uploads_total{outcome}` (`shared/metrics.py`).

## Тесты

Один контрактный набор на все адаптеры — `tests/test_pms_port.py` (R-7). Порт только
читает, поэтому у адаптера в наборе своя «выгрузка» (`_Harness.publish`); новый адаптер —
строкой в `ADAPTERS`: не прошёл контракт — дефект адаптера. Разбор файла —
`tests/test_pms_opera_parse.py`, маршрут, токен и сквозной путь до Stay —
`tests/test_pms_opera_report.py`.
