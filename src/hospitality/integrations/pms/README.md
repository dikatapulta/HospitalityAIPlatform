# integrations/pms — адаптеры источника проживаний

Реализации порта `PmsPort` (`shared/ports/pms.py`,
[spec 0058](../../../../docs/specs/0058-pms-stay-sync.md) §2.1,
[ADR-004](../../../../docs/adr/ADR-004-pms-stay-source-opera-report-snapshot.md)).
Подключает только composition root — `app.py` и `worker.py` зовут `build_pms()` и
кладут результат в порт (`set_pms`). Ядро и модули адаптеры не импортируют
(контракт 2 import-linter).

## Что умеет

Отдаёт последний принятый снимок проживаний тенанта: `fetch_snapshot(tenant_id)` —
`PmsSnapshot` (`source_id`, `taken_at` в UTC, строки `PmsStayRow`: комната, номер
брони, даты заезда и выезда по календарю отеля, число гостей или `None`) или `None`,
если снимков ещё не было. Имён гостей в снимке нет (ADR-004). Что делать со снимком —
работа сверки `modules/guests` (spec 0058 §2.2, PR 3 серии #122).

| Адаптер | Файл | Откуда снимок | Для чего |
|---|---|---|---|
| `FakePmsAdapter` | `fake.py` | память процесса: «дом отеля» двигают `check_in_room` / `check_out_booking` / `move_booking` / `set_departure`, снимок выпускает `publish()` | тесты, CI, локальный uvicorn (ADR-007) |
| адаптер Opera | `opera_report/` — PR 4 серии | выгрузка отчёта Opera, принятая маршрутом (spec 0058 §2.4) | staging, гостевая копия |

Правки «дома» Fake в снимок не попадают до `publish()`; каждый `publish()` — новый
`source_id`, даже если строки те же (так сверка отличает новый снимок от повтора).

## Конфигурация

Пока адаптер один, настройки нет: `build_pms()` отдаёт Fake. `PMS_BACKEND=fake|opera_report`
приходит вместе с адаптером Opera (spec 0058 §4, PR 4). Сверка идёт только у тенанта с
`TenantConfig.pms_source: opera_report` — у остальных снимок никто не читает.

## Health

`check()` — готов ли источник; Fake всегда `True`. В `/health/ready` порт не входит:
опоздавший снимок — алерт `ERR-GUESTS-010` (spec 0058 §2.6), а не отказ приложения.

## Тесты

Один контрактный набор на все адаптеры — `tests/test_pms_port.py` (R-7). Порт только
читает, поэтому у адаптера в наборе своя «выгрузка» (`_Harness.publish`); новый адаптер —
строкой в `ADAPTERS`: не прошёл контракт — дефект адаптера.
