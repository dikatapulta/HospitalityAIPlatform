# documents — реквизиты для закрывающих документов (АВР) от гостя

## Назначение

Гость присылает реквизиты своей компании — файлы (PDF, Word, фото) и текст, — бухгалтерия
отеля видит их строкой таблицы «Документы для АВР» с полями (БИН/ИИН, ИИК, БИК, банк,
адрес) и делает акт, как привыкла ([spec 0049](../../../../docs/specs/0049-act-requisites.md),
issue #493). Запрос — заявка в службе «Бухгалтерия» (`accounting`) плюс строка этого
модуля: статус, `#N`, очередь, лестница и «Готово» — у заявки, реквизиты — здесь (Р-1).

Поля из текста гостя и PDF/DOCX с текстовым слоем находит платформа без модели (§4), из
фото и сканов — пока бухгалтер (моделью — PR 5 серии). Каждая строка — «Проверить», пока
человек не нажал «Проверено» (NG-4, P-9). Реквизиты и текст гостя хранятся только
шифртекстом (FOUNDATION §11, Р-3).

## Состав

| Файл | Что даёт |
| --- | --- |
| `models.py` | `ActRequest`, `ActRequestFile` (тенантные, RLS-канон, миграция 0042); `ExtractionState`, `FileTextState` |
| `service.py` | Приём (`submit_act_request`), таблица, правка, «Проверено», выдача файла; коды ошибок |
| `processing.py` | Разбор в воркере, обезличивание по сроку, уборка брошенных |
| `extraction.py` | Поля из текста по шаблонам §4, контрольные суммы БИН/ИИН и IBAN — чистые функции |
| `codec.py` | Шифртексты строки ↔ значения: формат JSON и AAD — только здесь |
| `schemas.py` | Схемы границ; `ActingUser` — реэкспорт `StaffActor` |
| `events.py` | `act_request.submitted` |
| `api.py` | Публичный интерфейс (единственная точка входа извне) |
| `tests/` | Шифртекст в БД и чужой AAD, без ключа — ничего не сохранено, изоляция тенантов, повтор и гонка `client_key`, лимиты, разбор PDF/DOCX/скана/DTD, правка человека против повторного разбора, обезличивание, брошенные |

## Публичный API

- `submit_act_request(ActRequestSubmit, files) -> ActRequestRead` — порядок spec 0038 Р-3:
  файлы в узел (`store_upload(guest_document)`) → строка с `request_id = uuid4()` и событие
  одной транзакцией → `requests.create_request(request_id=…)` (`origin = guest_form`,
  `summary` «Реквизиты для АВР», `details` «Файлов: N» — текста гостя в заявке нет) →
  отметка `request_created`. Повтор с тем же `client_key` возвращает тот же запрос и
  доводит заявку; гонку решает UNIQUE, проигравший удаляет свои файлы. Отказ одного файла
  удаляет уже принятые. Номер и даты — снимок `Stay` (`guests.api.get_active_stay`).
  Выключатель контура `ENABLE_GUEST_DOCUMENTS` проверяет маршрут гостя (PR 4, Р-7) —
  доменные модули окружения не читают; домен отвечает ERR-DOCUMENTS-005, если у отеля
  нет категории `accounting`.
- `accepts_act_requests() -> bool` — есть ли у отеля служба «Бухгалтерия» (скрепка, PR 4).
- `list_act_requests(*, created_from, created_before, status=None) -> list[ActRequestRow]` —
  строки страницы с расшифрованными полями, подсказками (`checksum`, `ambiguous`) и файлами.
- `update_requisites(id, RequisitesUpdate, *, acting_user)` — формат строго (§4,
  ERR-DOCUMENTS-006), поле попадает в `edited_fields`, отметка «Проверено» снимается.
- `mark_requisites_checked(id, *, acting_user)`; `read_act_request_file(id, file_id)` —
  только файл этого запроса; `get_act_request_by_request(request_id)`.
- `extract_requisites(id)` / `handle_act_request_submitted(event)` — разбор (Р-4): текст
  PDF и DOCX достаёт дочерний процесс песочницы (`platform/file_sandbox.extract_text_isolated`),
  поля ищет `extraction.py`, запись — под `FOR UPDATE`. Поля из `edited_fields` и строку
  «Проверено» разбор не трогает. Битый файл — `failed` у файла и у строки; сбой ключа или
  хранилища — исключение, outbox повторит.
- `anonymize_expired_act_requests(*, created_before)` — шифртексты и имя проверившего →
  NULL, строка живёт (spec 0032). `delete_abandoned_act_requests(*, created_before)` —
  запрос без заявки старше `ABANDONED_AFTER` (сутки) удаляется с файлами; заявка всё-таки
  есть (процесс упал до отметки) — ставится отметка. Обе зовёт `channels/common/retention.py`.

## События

- Публикует: `act_request.submitted` (`act_request_id`) — в транзакции строки; в payload
  ни реквизитов, ни текста.
- Потребляет: своё `act_request.submitted` — подписчик разбора, регистрирует `worker.py`.
  Сообщения гостю («Готово», «Вернули») — подписчик канала на `request.status_changed`
  (PR 4, Р-2).

## Таблицы

`act_requests` и `act_request_files` (миграция 0042, RLS-канон 0002). Открыты: номер,
даты, язык, состояния, имена правленых полей (`edited_fields`). Шифртекстом:
`guest_input_ciphertext` (`bill_to`, `note`) и `requisites_ciphertext` (поля и список
«несколько вариантов») — AES-256-GCM ключом `FILE_ENCRYPTION_KEY`, AAD
`{tenant_id}/act_requests/{id}/{колонка}` (`platform/field_encryption.py`). Ключа нет —
ERR-FILES-005: ни строка, ни файлы не пишутся. `act_request_files.file_id` →
`uploaded_files` ON DELETE CASCADE. ПД — строки `docs/PII_REGISTRY.md`.

## Зависимости

Внутренние: `modules/requests` и `modules/guests` — только через `api.py`;
`platform/files` (узел файлов), `platform/file_sandbox` и `platform/field_encryption`,
`platform/staff_services` (`ACCOUNTING_SERVICE_KEY`), `shared`. Внешняя — `pypdf` (только
в дочернем процессе песочницы).

## Типовые сценарии изменения

- Новое поле реквизитов → значение `RequisiteField`, поле `Requisites`/`RequisitesUpdate`,
  шаблон и проверка в `extraction.py`; миграция не нужна — поля живут в шифртексте.
- Фото и сканы моделью (PR 5) → подписчик композиционного слоя зовёт `ai/gateway` и
  пишет черновик полей через `api.py` (доменный модуль `ai/` не импортирует); та же
  проверка формата и «Проверить».
- Карточка компании по БИН (`CompanyProfile`, Phase 3) рождается из проверенной строки,
  второй таблицы тех же реквизитов нет (Р-6).
