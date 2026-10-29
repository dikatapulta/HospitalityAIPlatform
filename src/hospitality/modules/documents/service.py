"""Бизнес-логика модуля documents: приём запроса, таблица, правка (spec 0049).

Копия канона `modules/requests/service.py`: каждая функция вызывается внутри
`tenant_context` и открывает `session_scope()`; событие — в транзакции строки
(P-6). Разбор, обезличивание и уборка брошенных — `processing.py`.

Запись запроса — порядком spec 0038 Р-3: файлы в узел → строка с
`request_id = uuid4()` и событие → `requests.create_request(request_id=…)`,
идемпотентный по id. Сбой между шагами — строка без заявки: повтор с тем же
`client_key` доводит дело, брошенную снимает ретеншн через сутки.

Права (кто видит страницу и правит) режет кабинет (PR 3), выключатель контура
`ENABLE_GUEST_DOCUMENTS` — маршрут гостя (PR 4, Р-7): доменные модули настроек
окружения не читают. Домен держит то, что есть у отеля в данных, — категорию
`accounting`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.documents.codec import (
    GuestInput,
    StoredRequisites,
    decrypt_guest_input,
    decrypt_requisites,
    encrypt_guest_input,
    encrypt_requisites,
)
from hospitality.modules.documents.events import ActRequestSubmitted
from hospitality.modules.documents.extraction import field_warnings, normalize_field
from hospitality.modules.documents.journal import journal_record
from hospitality.modules.documents.models import ActRequest, ActRequestFile
from hospitality.modules.documents.schemas import (
    MAX_FILES,
    MAX_REQUESTS_PER_STAY,
    ActingUser,
    ActRequestFileRead,
    ActRequestRead,
    ActRequestRow,
    ActRequestSubmit,
    FieldWarning,
    RequisiteField,
    RequisitesStatus,
    RequisitesUpdate,
    UploadInput,
)
from hospitality.modules.guests import api as guests_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.files import (
    FilePurpose,
    StoredFile,
    UploadedFile,
    delete_file,
    read_file,
    store_upload,
)
from hospitality.platform.staff_actions import StaffAction, record_staff_action
from hospitality.platform.staff_services import ACCOUNTING_SERVICE_KEY
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.events import publish
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_act_request
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_DOCUMENTS_EMPTY = "ERR-DOCUMENTS-001"
ERR_DOCUMENTS_TOO_MANY_FILES = "ERR-DOCUMENTS-002"
ERR_DOCUMENTS_STAY_LIMIT = "ERR-DOCUMENTS-003"
ERR_DOCUMENTS_NOT_FOUND = "ERR-DOCUMENTS-004"
ERR_DOCUMENTS_DISABLED = "ERR-DOCUMENTS-005"
ERR_DOCUMENTS_FIELD_FORMAT = "ERR-DOCUMENTS-006"


class ActRequestFileError(AppError):
    """Отказ узла файлов на одном файле формы: код узла и номер файла с нуля.

    Страница гостя называет файл по номеру («Этот формат не подойдёт: …», §6).
    """

    def __init__(self, error: AppError, *, position: int) -> None:
        super().__init__(code=error.code, message=error.message, status_code=error.status_code)
        self.position = position


# Заявка в очереди (Р-1): текста гостя в ней нет — только суть и число файлов.
REQUEST_SUMMARY: Final = "Реквизиты для АВР"
_CLIENT_KEY_CONSTRAINT: Final = "uq_act_requests_stay_client_key"


async def accepts_act_requests() -> bool:
    """Есть ли у отеля служба «Бухгалтерия» — без неё запрос некуда отдать (Р-7)."""
    return await _accounting_category_id() is not None


async def submit_act_request(data: ActRequestSubmit, files: list[UploadInput]) -> ActRequestRead:
    """Принять запрос гостя: файлы, строка, событие разбора, заявка в «Бухгалтерию».

    Повтор с тем же `client_key` возвращает тот же запрос (и доводит заявку,
    если её не успели создать). Отказ одного файла удаляет уже принятые файлы
    этого запроса; гонка двух отправок одного ключа — проигравший удаляет свои.
    """
    category_id = await _accounting_category_id()
    if category_id is None:
        raise AppError(
            code=ERR_DOCUMENTS_DISABLED, message="Приём реквизитов выключен.", status_code=404
        )
    guest_input = GuestInput(bill_to=data.bill_to.strip(), note=data.note.strip())
    if not guest_input.bill_to and not guest_input.note and not files:
        raise _rejected(ERR_DOCUMENTS_EMPTY, "Пустая форма.", 422)
    if len(files) > MAX_FILES:
        raise _rejected(ERR_DOCUMENTS_TOO_MANY_FILES, "Больше 5 файлов.", 413)
    existing = await _find_by_client_key(data.stay_id, data.client_key)
    if existing is not None:
        record_act_request("duplicate")
        return await _ensure_request(existing, category_id)
    stay = await guests_api.get_active_stay(data.stay_id)
    if stay is None:
        raise _rejected(ERR_DOCUMENTS_NOT_FOUND, "Проживание не найдено.", 404)
    async with session_scope() as session:
        sent = await session.scalar(
            select(func.count()).select_from(ActRequest).where(ActRequest.stay_id == stay.id)
        )
    if (sent or 0) >= MAX_REQUESTS_PER_STAY:
        raise _rejected(ERR_DOCUMENTS_STAY_LIMIT, "Лимит запросов проживания.", 429)

    row = ActRequest(
        id=uuid.uuid4(),
        tenant_id=current_tenant_id(),
        request_id=uuid.uuid4(),
        request_created=False,
        stay_id=stay.id,
        guest_identity_id=data.guest_identity_id,
        client_key=data.client_key,
        guest_language=data.language,
        room_number=stay.room_number,
        check_in_at=stay.check_in_at,
        check_out_at=stay.check_out_at,
        edited_fields=[],
    )
    # Шифрование — до файлов: без ключа не остаётся ни файлов, ни строки.
    row.guest_input_ciphertext = encrypt_guest_input(row, guest_input)
    stored = await _store_files(files, data.guest_identity_id)
    try:
        async with session_scope() as session:
            session.add(row)
            await session.flush()
            for position, file_id in enumerate(stored):
                session.add(
                    ActRequestFile(
                        tenant_id=row.tenant_id,
                        act_request_id=row.id,
                        file_id=file_id,
                        position=position,
                    )
                )
            await publish(session, ActRequestSubmitted(act_request_id=row.id))
    except IntegrityError as error:
        await delete_files_quietly(stored)
        if _CLIENT_KEY_CONSTRAINT not in str(error):
            raise
        winner = await _find_by_client_key(data.stay_id, data.client_key)
        if winner is None:
            raise
        record_act_request("duplicate")
        return await _ensure_request(winner, category_id)
    except BaseException:
        await delete_files_quietly(stored)
        raise
    record_act_request("created")
    return await _ensure_request(row, category_id, files_count=len(stored))


async def get_act_request_by_request(request_id: uuid.UUID) -> ActRequestRead | None:
    """Запрос по id его заявки (карточка очереди, подписчик «Готово» PR 4)."""
    async with session_scope() as session:
        row = await session.scalar(select(ActRequest).where(ActRequest.request_id == request_id))
        if row is None:
            return None
        return _read(row, await _files_count(row.id))


async def list_act_requests(
    *,
    created_from: datetime,
    created_before: datetime,
    status: RequisitesStatus | None = None,
    limit: int | None = None,
) -> list[ActRequestRow]:
    """Строки страницы «Документы для АВР» за период, новые сверху, с расшифрованными полями.

    `limit` — потолок страницы: строка живёт и после обезличивания (spec 0032),
    и длинный период без потолка читал бы их все.
    """
    query = select(ActRequest).where(
        ActRequest.created_at >= created_from, ActRequest.created_at < created_before
    )
    if status is RequisitesStatus.CHECKED:
        query = query.where(ActRequest.checked_at.is_not(None))
    elif status is RequisitesStatus.TO_CHECK:
        query = query.where(ActRequest.checked_at.is_(None))
    async with session_scope() as session:
        query = query.order_by(ActRequest.created_at.desc(), ActRequest.id)
        if limit is not None:
            query = query.limit(limit)
        rows = list(await session.scalars(query))
        files = await _files_by_request(session, [row.id for row in rows])
    return [_row(row, files.get(row.id, [])) for row in rows]


async def update_requisites(
    act_request_id: uuid.UUID, changes: RequisitesUpdate, *, acting_user: ActingUser
) -> ActRequestRow:
    """Правка бухгалтера: формат строго (ERR-DOCUMENTS-006), поле — «правил человек».

    Любая правка снимает «Проверено» (Р-5): отметка значит «эти значения сверил
    человек». Повторный разбор правленые поля не трогает (Р-4).
    """
    normalized: dict[RequisiteField, str] = {}
    for name, raw in changes.model_dump(exclude_none=True).items():
        field = RequisiteField(name)
        value = normalize_field(field, raw)
        if value is None:
            raise _rejected(ERR_DOCUMENTS_FIELD_FORMAT, f"Неверный формат поля {field}.", 422)
        normalized[field] = value
    if not normalized:
        # Нечего править — ни отметку снимать, ни журнал писать не за что.
        return await _current_row(act_request_id)
    journal = await journal_record(act_request_id, StaffAction.ACT_REQUISITES_EDITED)
    if journal is None:
        raise _rejected(ERR_DOCUMENTS_NOT_FOUND, "Запрос не найден.", 404)
    async with session_scope() as session:
        row = await _editable_row(session, act_request_id)
        stored = decrypt_requisites(row)
        values = stored.values.model_copy(update={f.value: v for f, v in normalized.items()})
        row.requisites_ciphertext = encrypt_requisites(
            row, StoredRequisites(values, stored.ambiguous - normalized.keys())
        )
        row.edited_fields = sorted(set(row.edited_fields) | {f.value for f in normalized})
        row.checked_at = None
        row.checked_by_user_id = None
        row.checked_by_display_name = None
        # Журнал — той же сессией, что правка (spec 0041 §2.1): откат правки
        # откатывает и строку.
        record_staff_action(session, acting_user, journal)
        await session.flush()
        files = await _files_by_request(session, [row.id])
        result = _row(row, files.get(row.id, []))
    logger.info(
        "act_requisites_edited",
        act_request_id=str(act_request_id),
        fields=sorted(f.value for f in normalized),
        user_id=str(acting_user.user_id),
    )
    return result


async def mark_requisites_checked(
    act_request_id: uuid.UUID, *, acting_user: ActingUser
) -> ActRequestRow:
    """«Проверено» (Р-5): человек сверил значения строки."""
    journal = await journal_record(act_request_id, StaffAction.ACT_REQUISITES_CHECKED)
    if journal is None:
        raise _rejected(ERR_DOCUMENTS_NOT_FOUND, "Запрос не найден.", 404)
    async with session_scope() as session:
        row = await _editable_row(session, act_request_id)
        row.checked_at = utc_now()
        row.checked_by_user_id = acting_user.user_id
        row.checked_by_display_name = acting_user.display_name
        record_staff_action(session, acting_user, journal)
        await session.flush()
        files = await _files_by_request(session, [row.id])
        result = _row(row, files.get(row.id, []))
    logger.info(
        "act_requisites_checked",
        act_request_id=str(act_request_id),
        user_id=str(acting_user.user_id),
    )
    return result


async def read_act_request_file(act_request_id: uuid.UUID, file_id: uuid.UUID) -> StoredFile:
    """Файл этого запроса; файл другого запроса или чужой — ERR-DOCUMENTS-004."""
    async with session_scope() as session:
        linked = await session.scalar(
            select(ActRequestFile.id).where(
                ActRequestFile.act_request_id == act_request_id,
                ActRequestFile.file_id == file_id,
            )
        )
    if linked is None:
        raise _rejected(ERR_DOCUMENTS_NOT_FOUND, "Файл не найден.", 404)
    return await read_file(file_id)


async def _accounting_category_id() -> uuid.UUID | None:
    for category in await requests_api.list_categories():
        if category.key == ACCOUNTING_SERVICE_KEY:
            return category.id
    return None


async def _find_by_client_key(stay_id: uuid.UUID, client_key: str) -> ActRequest | None:
    async with session_scope() as session:
        found: ActRequest | None = await session.scalar(
            select(ActRequest).where(
                ActRequest.stay_id == stay_id, ActRequest.client_key == client_key
            )
        )
    return found


async def _ensure_request(
    row: ActRequest, category_id: uuid.UUID, *, files_count: int | None = None
) -> ActRequestRead:
    """Заявка запроса (идемпотентно по `request_id`) и отметка «заявка есть»."""
    if files_count is None:
        files_count = await _files_count(row.id)
    if not row.request_created:
        await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category_id,
                origin=requests_api.ServiceRequestOrigin.GUEST_FORM,
                summary=REQUEST_SUMMARY,
                details=f"Файлов: {files_count}",
                room_number=row.room_number,
                guest_language=row.guest_language,
            ),
            request_id=row.request_id,
        )
        async with session_scope() as session:
            await session.execute(
                update(ActRequest).where(ActRequest.id == row.id).values(request_created=True)
            )
        logger.info(
            "act_request_submitted",
            act_request_id=str(row.id),
            request_id=str(row.request_id),
            files=files_count,
        )
    return _read(row, files_count)


async def _store_files(files: list[UploadInput], guest_identity_id: uuid.UUID) -> list[uuid.UUID]:
    stored: list[uuid.UUID] = []
    try:
        for upload in files:
            meta = await store_upload(
                FilePurpose.GUEST_DOCUMENT,
                upload.data,
                upload.filename,
                guest_identity_id=guest_identity_id,
            )
            stored.append(meta.id)
    except AppError as error:
        record_act_request("rejected")
        await delete_files_quietly(stored)
        raise ActRequestFileError(error, position=len(stored)) from error
    except BaseException:
        record_act_request("rejected")
        await delete_files_quietly(stored)
        raise
    return stored


async def delete_files_quietly(file_ids: list[uuid.UUID]) -> None:
    """Удалить файлы узла; не удалился — WARNING, а не исключение поверх исходного."""
    for file_id in file_ids:
        try:
            await delete_file(file_id)
        except Exception:
            # Не удалился — файл без связи доживёт до ретеншна узла (срок spec 0032).
            logger.warning("act_request_file_cleanup_failed", file_id=str(file_id), exc_info=True)


async def _files_count(act_request_id: uuid.UUID) -> int:
    async with session_scope() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(ActRequestFile)
            .where(ActRequestFile.act_request_id == act_request_id)
        )
    return count or 0


async def _files_by_request(
    session: AsyncSession, act_request_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[ActRequestFileRead]]:
    if not act_request_ids:
        return {}
    rows = await session.execute(
        select(ActRequestFile, UploadedFile)
        .join(UploadedFile, UploadedFile.id == ActRequestFile.file_id)
        .where(ActRequestFile.act_request_id.in_(act_request_ids))
        .order_by(ActRequestFile.position)
    )
    result: dict[uuid.UUID, list[ActRequestFileRead]] = {}
    for link, meta in rows.tuples():
        result.setdefault(link.act_request_id, []).append(
            ActRequestFileRead(
                file_id=link.file_id,
                position=link.position,
                original_name=meta.original_name,
                content_type=meta.content_type,
                text_state=link.text_state,
            )
        )
    return result


async def _current_row(act_request_id: uuid.UUID) -> ActRequestRow:
    async with session_scope() as session:
        row = await _editable_row(session, act_request_id)
        files = await _files_by_request(session, [row.id])
        return _row(row, files.get(row.id, []))


async def _editable_row(session: AsyncSession, act_request_id: uuid.UUID) -> ActRequest:
    row: ActRequest | None = await session.scalar(
        select(ActRequest).where(ActRequest.id == act_request_id).with_for_update()
    )
    if row is None or row.anonymized_at is not None:
        raise _rejected(ERR_DOCUMENTS_NOT_FOUND, "Запрос не найден.", 404)
    return row


def _read(row: ActRequest, files_count: int) -> ActRequestRead:
    return ActRequestRead(
        id=row.id,
        request_id=row.request_id,
        stay_id=row.stay_id,
        guest_identity_id=row.guest_identity_id,
        room_number=row.room_number,
        check_in_at=row.check_in_at,
        check_out_at=row.check_out_at,
        guest_language=row.guest_language,
        files_count=files_count,
        created_at=row.created_at,
    )


def _row(row: ActRequest, files: list[ActRequestFileRead]) -> ActRequestRow:
    # У обезличенной строки шифртекстов нет — оба decrypt отдают пустое.
    guest_input = decrypt_guest_input(row)
    stored = decrypt_requisites(row)
    warnings = field_warnings(stored.values)
    warnings.update(dict.fromkeys(stored.ambiguous, FieldWarning.AMBIGUOUS))
    return ActRequestRow(
        id=row.id,
        request_id=row.request_id,
        room_number=row.room_number,
        check_in_at=row.check_in_at,
        check_out_at=row.check_out_at,
        guest_language=row.guest_language,
        bill_to=guest_input.bill_to,
        note=guest_input.note,
        requisites=stored.values,
        warnings=warnings,
        edited_fields=[RequisiteField(name) for name in row.edited_fields],
        extraction_state=row.extraction_state,
        status=RequisitesStatus.CHECKED if row.checked_at else RequisitesStatus.TO_CHECK,
        checked_at=row.checked_at,
        checked_by_display_name=row.checked_by_display_name,
        anonymized=row.anonymized_at is not None,
        files=files,
        created_at=row.created_at,
    )


def _rejected(code: str, message: str, status_code: int) -> AppError:
    return AppError(code=code, message=message, status_code=status_code)
