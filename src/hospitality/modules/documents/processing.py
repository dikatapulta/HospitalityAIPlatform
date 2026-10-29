"""Фоновая жизнь запроса реквизитов: разбор, обезличивание, брошенные (spec 0049 Р-4, §3).

Разбор (`extract_requisites`) — первый шаг подписчика `act_request.submitted` в
медленной полосе воркера (issue #511; подписчик — `ai/act_requisites.py`, второй
его шаг — фото моделью, `scans.py`): файлы читает узел, текст PDF и DOCX достаёт дочерний
процесс песочницы, поля ищет `extraction.parse_requisites`. Тяжёлая часть — без
блокировки строки; запись — под `FOR UPDATE`, чтобы не затереть правку
бухгалтера, сделанную, пока шёл разбор. Повтор безопасен (P-8): правленые поля и
строка «Проверено» не трогаются.

Ошибка одного файла — вердикт о нём (`failed`), остальные файлы и текст гостя
разбираются (Р-4). Ошибка среды — исключение, outbox повторит, а исчерпав
повторы, расскажет человеку алертом: какая из двух, решает проба в момент
ошибки (`_file_text`).

Ретеншн (`channels/common/retention.py`, шаг 8) зовёт две функции: обезличить
строки старше срока spec 0032 (шифртексты → NULL, строка живёт) и удалить
брошенные — запрос без заявки старше суток вместе с файлами.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Final, cast

from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult

from hospitality.modules.documents.codec import (
    StoredRequisites,
    decrypt_guest_input,
    decrypt_requisites,
    encrypt_requisites,
)
from hospitality.modules.documents.extraction import parse_requisites
from hospitality.modules.documents.models import (
    ActRequest,
    ActRequestFile,
    ExtractionState,
    FileTextState,
)
from hospitality.modules.documents.schemas import RequisiteField, Requisites
from hospitality.modules.documents.service import delete_files_quietly
from hospitality.modules.requests import api as requests_api
from hospitality.platform.file_sandbox import (
    SandboxFailedError,
    check_sandbox,
    extract_text_isolated,
)
from hospitality.platform.files import (
    ERR_FILE_KEY,
    ERR_FILE_NOT_FOUND,
    ERR_FILE_STORAGE,
    read_file,
)
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.health import check_file_storage
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Запрос без заявки старше этого — брошенный (spec 0049 §3, spec 0038 Р-3):
# повтор гостя с тем же ключом за сутки ещё доводит дело.
ABANDONED_AFTER: Final = timedelta(days=1)
# Отказы узла при чтении файла запроса: каждый может быть и о файле, и о среде.
_READ_FAILURES: Final = frozenset({ERR_FILE_NOT_FOUND, ERR_FILE_KEY, ERR_FILE_STORAGE})


async def extract_requisites(act_request_id: uuid.UUID) -> None:
    """Разобрать запрос: состояния файлов и поля, кроме правленых человеком."""
    async with session_scope() as session:
        row = await session.get(ActRequest, act_request_id)
        if row is None or row.anonymized_at is not None:
            logger.info("act_requisites_extraction_skipped", act_request_id=str(act_request_id))
            return
        links = list(
            await session.scalars(
                select(ActRequestFile)
                .where(ActRequestFile.act_request_id == act_request_id)
                .order_by(ActRequestFile.position)
            )
        )
    guest_input = decrypt_guest_input(row)
    states: dict[uuid.UUID, FileTextState] = {}
    texts: list[str] = []
    for link in links:
        states[link.id], text = await _file_text(link.file_id)
        if text:
            texts.append(text)
    parsed = parse_requisites(guest_input.bill_to, [guest_input.note, *texts])

    async with session_scope() as session:
        locked = await session.scalar(
            select(ActRequest).where(ActRequest.id == act_request_id).with_for_update()
        )
        if locked is None or locked.anonymized_at is not None:
            return
        # Шифртекст — до записи и всегда, даже у «Проверено»: ключа нет — исключение
        # раньше, чем файлы получат `failed`, которого они не заслужили.
        ciphertext = encrypt_requisites(
            locked, _merge(decrypt_requisites(locked), parsed.values, parsed.ambiguous, locked)
        )
        for link_id, state in states.items():
            await session.execute(
                update(ActRequestFile).where(ActRequestFile.id == link_id).values(text_state=state)
            )
        if locked.checked_at is None:
            locked.requisites_ciphertext = ciphertext
        failed = FileTextState.FAILED in states.values()
        locked.extraction_state = ExtractionState.FAILED if failed else ExtractionState.DONE
        filled = sum(1 for value in parsed.values.model_dump().values() if value)
    logger.info(
        "act_requisites_extracted",
        act_request_id=str(act_request_id),
        files=len(links),
        fields_found=filled,
        ambiguous=len(parsed.ambiguous),
        files_failed=sum(1 for state in states.values() if state is FileTextState.FAILED),
    )


async def anonymize_expired_act_requests(*, created_before: datetime) -> int:
    """Обезличить запросы старше срока (spec 0032): шифртексты и имя проверившего — NULL.

    Строка живёт: номер, даты, статусы — для истории таблицы. Файлы снимает
    ретеншн узла (`delete_expired_files`), связь уходит каскадом.
    """
    async with session_scope() as session:
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                update(ActRequest)
                .where(ActRequest.created_at < created_before, ActRequest.anonymized_at.is_(None))
                .values(
                    guest_input_ciphertext=None,
                    requisites_ciphertext=None,
                    checked_by_display_name=None,
                    anonymized_at=utc_now(),
                )
            ),
        )
    return result.rowcount or 0


async def delete_abandoned_act_requests(*, created_before: datetime) -> int:
    """Удалить запросы без заявки старше `created_before` вместе с файлами (§3).

    «Без заявки» сверяется с модулем requests: процесс мог упасть между
    созданием заявки и отметкой `request_created` — такая строка получает
    отметку, а не удаляется.
    """
    async with session_scope() as session:
        candidates = list(
            await session.scalars(
                select(ActRequest).where(
                    ActRequest.request_created.is_(False), ActRequest.created_at < created_before
                )
            )
        )
    deleted = 0
    for row in candidates:
        try:
            await requests_api.get_request(row.request_id)
        except AppError as exc:
            if exc.code != requests_api.ERR_REQUESTS_REQUEST_NOT_FOUND:
                raise
        else:
            async with session_scope() as session:
                await session.execute(
                    update(ActRequest).where(ActRequest.id == row.id).values(request_created=True)
                )
            continue
        async with session_scope() as session:
            file_ids = list(
                await session.scalars(
                    select(ActRequestFile.file_id).where(ActRequestFile.act_request_id == row.id)
                )
            )
        await delete_files_quietly(file_ids)
        async with session_scope() as session:
            await session.execute(delete(ActRequest).where(ActRequest.id == row.id))
        deleted += 1
        logger.info("act_request_abandoned_deleted", act_request_id=str(row.id))
    return deleted


async def _file_text(file_id: uuid.UUID) -> tuple[FileTextState, str]:
    """Текст файла; ошибка о файле — `failed`, ошибка среды — исключение (issue #511).

    О файле: его нет (ретеншн узла), шифртекст не расшифровывается, объекта нет
    на исправном томе, ребёнка песочницы убил лимит. О среде: хранилище не
    проходит `check_file_storage`, песочница не отвечает и на пустом входе;
    ключа нет совсем — запись полей без ключа падает (`extract_requisites`).
    """
    try:
        stored = await read_file(file_id)
    except AppError as exc:
        if exc.code not in _READ_FAILURES:
            raise
        if exc.code == ERR_FILE_STORAGE and not await check_file_storage():
            raise
        logger.warning("act_file_unreadable", file_id=str(file_id), error_code=exc.code)
        return FileTextState.FAILED, ""
    if stored.meta.content_type.startswith("image/"):
        return FileTextState.IMAGE, ""
    try:
        extracted = await extract_text_isolated(stored.data)
    except SandboxFailedError as exc:
        await check_sandbox()  # мертва и на пустом входе — исключение, а не вердикт о файле
        logger.warning("act_file_sandbox_failed", file_id=str(file_id), detail=str(exc))
        return FileTextState.FAILED, ""
    return FileTextState(extracted.state.value), extracted.text


def _merge(
    current: StoredRequisites,
    parsed: Requisites,
    ambiguous: frozenset[RequisiteField],
    row: ActRequest,
) -> StoredRequisites:
    """Найденное разбором — во все поля, кроме правленых человеком (Р-4)."""
    edited = {RequisiteField(name) for name in row.edited_fields}
    values = current.values.model_dump()
    for field in RequisiteField:
        if field not in edited:
            values[field.value] = getattr(parsed, field.value)
    return StoredRequisites(Requisites.model_validate(values), ambiguous - edited)
