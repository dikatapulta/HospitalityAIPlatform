# housekeeping — уборка номера после выезда

## Назначение

Путь одного номера от выезда до «готов к заселению» ([spec 0046](../../../../docs/specs/0046-checkout-turnover.md),
issue #476): ресепшен отмечает выезд по номеру комнаты, хаускипинг проверяет (с
замечанием или без), убирает, старший хаускипинга принимает. Отдельный модуль, а не
категория заявок — [ADR-021](../../../../docs/adr/ADR-021-room-turnover-module.md): путь
рождает факт выезда, а не просьба, он живёт по номеру комнаты и у него есть приёмка;
100–150 выездов в сутки не должны занимать дневные номера и отчёты заявок.

Кабинет ведёт работу, а не статус номера — тот остаётся в OPERA у менеджера
хаускипинга (NG-2). Модуль не знает ни о ролях, ни о Stay: кто какой шаг вправе
нажать и выселение гостя — забота кабинета (`staff_portal/turnovers.py`); здесь —
порядок шагов и запись «кто/когда».

## Состав

| Файл | Что даёт |
| --- | --- |
| `models.py` | `RoomTurnover` (тенантная, RLS-канон, миграция 0035), `TurnoverStatus`; partial UNIQUE «один открытый путь на номер» |
| `service.py` | `report_checkout`, `change_turnover_status` с картой `TURNOVER_TRANSITIONS`, чтения доски, «сегодня убрано», ретеншн замечаний; коды ошибок |
| `schemas.py` | `RoomTurnoverRead`, `TurnoverReport`, `CleanedByStaff`; `ActingUser` — реэкспорт `StaffActor` |
| `events.py` | `room_turnover.reported`, `room_turnover.status_changed` |
| `api.py` | Публичный интерфейс (единственная точка входа извне) |
| `tests/` | Шаги и порядок, повторная отметка и гонка, изоляция тенантов, границы списков и счёта, ретеншн |

## Публичный API

- `report_checkout(room_number, *, acting_user=None) -> TurnoverReport` — новый путь в
  `reported` + `room_turnover.reported`. У номера уже есть открытый путь — он и
  возвращается (`created=False`), без строки и события. Гонку двух отметок решает
  partial UNIQUE: проигравший перечитывает путь победителя.
- `change_turnover_status(turnover_id, status, *, acting_user=None, inspection_note=None)
  -> RoomTurnoverRead` — следующий шаг под `FOR UPDATE` + `room_turnover.status_changed`.
  Порядок строгий: `reported → inspected → cleaning → cleaned → ready`; `cancelled` — из
  любого открытого. Не по порядку — `ERR-HOUSEKEEPING-002` (409), нет пути —
  `ERR-HOUSEKEEPING-001` (404). «Когда» пишется всегда, «кто» — только с личностью
  (канон spec 0035 §3). `inspection_note` — только на переходе в `inspected`.
- `get_turnover(turnover_id)`; `list_open_turnovers(*, limit)` — открытые, давние сверху;
  `list_turnovers_ready_since(ready_after, *, limit)` — принятые с границы, свежие сверху
  (полночь отеля считает вызывающий).
- `count_cleaned_since(cleaned_after) -> list[CleanedByStaff]` — кто сколько раз нажал
  «Убрано» с границы, без отменённых путей.
- `clear_expired_inspection_notes(*, created_before) -> int` — замечания старше границы →
  NULL (spec 0046 §8; зовёт `channels/common/retention.py`). Идемпотентна.
- `TURNOVER_TRANSITIONS`, `OPEN_TURNOVER_STATUSES`, `TurnoverStatus` — для подписей и
  кнопок кабинета.

## События

- Публикует: `room_turnover.reported` (`turnover_id`, `room_number`),
  `room_turnover.status_changed` (+ `old_status`, `new_status`). Имён и замечания в
  payload нет. Подписчиков пока нет — вход будущей синхронизации с OPERA через порт PMS.
- Потребляет: ничего.

## Таблицы

`room_turnovers` (миграция 0035, RLS-канон 0002): номер, статус, замечание, по каждому
шагу — момент, FK на `users` (`ON DELETE SET NULL`) и снапшот имени. Partial UNIQUE
`uq_room_turnovers_tenant_room_open` по открытым статусам. PII — строки в
`docs/PII_REGISTRY.md`.

## Зависимости

Внутренние: `hospitality.shared` (Base/UTCDateTime/utc_now, session_scope,
DomainEvent/publish, AppError, логирование), `hospitality.platform.staff_actions`
(`StaffActor` — личность сотрудника). Внешних сверх общих нет.

## Типовые сценарии изменения

- Новый шаг пути → значение `TurnoverStatus`, строка карты `TURNOVER_TRANSITIONS`, колонки
  шага миграцией, ветка `_stamp_step`, условие partial UNIQUE в модели и миграции —
  оба места разом.
- Синхронизация с OPERA → подписчик `room_turnover.status_changed` в composition root
  воркера, вызов порта PMS (P-3); модуль не меняется.
- Этажи хаускипинга (#478) → своя таблица здесь же.
