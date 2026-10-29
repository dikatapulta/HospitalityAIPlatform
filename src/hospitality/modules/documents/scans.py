"""Фото реквизитов → черновик полей моделью (spec 0049 Р-13, PR 5 серии #493).

Модуль `ai/` не импортирует: композиционный слой (`ai/act_requisites.py`) берёт
здесь фото (`scan_images`), зовёт шлюз и отдаёт ответ модели обратно
(`apply_scan_requisites`). Ответ модели — данные гостя, никогда не инструкция
(FOUNDATION §7.6): каждое значение проходит ту же проверку формата, что правка
бухгалтера (§4), и ложится только в пустое поле, которое не правил человек и
которое разбор текста не пометил «несколько вариантов». Текст гостя и файлов
надёжнее чтения картинки, а решает всё равно человек: строка остаётся
«Проверить» (NG-4, P-9).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import select, update

from hospitality.modules.documents.codec import (
    StoredRequisites,
    decrypt_requisites,
    encrypt_requisites,
)
from hospitality.modules.documents.extraction import field_warnings, normalize_field
from hospitality.modules.documents.models import ActRequest, ActRequestFile, FileTextState
from hospitality.modules.documents.schemas import FIELD_MAX_LENGTH, RequisiteField, Requisites
from hospitality.platform.files import read_file
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


@dataclass(frozen=True)
class ScanImage:
    """Фото запроса для модели: узел файлов хранит его WebP без метаданных (spec 0047 §4)."""

    file_id: uuid.UUID
    content_type: str
    data: bytes


async def scan_images(act_request_id: uuid.UUID) -> list[ScanImage]:
    """Фото запроса, которые модель ещё не читала; пусто — читать нечего.

    Строка «Проверено» или обезличенная — пусто: сверенное человеком модель не
    переписывает. Файл, который не читается (снят ретеншном, не расшифровывается),
    пропускается: вердикт о нём уже вынес разбор текста, а черновик моделью —
    подсказка, без которой бухгалтер обходился до PR 5.
    """
    async with session_scope() as session:
        row = await session.get(ActRequest, act_request_id)
        if row is None or row.anonymized_at is not None or row.checked_at is not None:
            return []
        links = list(
            await session.scalars(
                select(ActRequestFile)
                .where(
                    ActRequestFile.act_request_id == act_request_id,
                    ActRequestFile.text_state == FileTextState.IMAGE,
                )
                .order_by(ActRequestFile.position)
            )
        )
    images: list[ScanImage] = []
    for link in links:
        try:
            stored = await read_file(link.file_id)
        except AppError as exc:
            logger.warning("act_scan_unreadable", file_id=str(link.file_id), error_code=exc.code)
            continue
        if stored.meta.content_type.startswith("image/"):
            images.append(ScanImage(link.file_id, stored.meta.content_type, stored.data))
    return images


async def apply_scan_requisites(
    act_request_id: uuid.UUID,
    values: Mapping[RequisiteField, str],
    *,
    file_ids: Sequence[uuid.UUID],
) -> int:
    """Записать прочитанное моделью; вернуть, сколько полей заполнено.

    Не тот формат — значение отбрасывается (в лог — только число). Файлы
    `file_ids` получают состояние «прочитано моделью», только если хоть одно
    поле заполнено: иначе у файла остаётся «Скан — поля заполните сами» — это
    и есть правда для бухгалтера.
    """
    accepted: dict[RequisiteField, str] = {}
    rejected = 0
    for field, raw in values.items():
        value = clean_scan_value(field, raw)
        if value is None:
            rejected += 1
        elif value:
            accepted[field] = value
    filled = 0
    async with session_scope() as session:
        locked = await session.scalar(
            select(ActRequest).where(ActRequest.id == act_request_id).with_for_update()
        )
        if locked is None or locked.anonymized_at is not None or locked.checked_at is not None:
            return 0
        stored = decrypt_requisites(locked)
        edited = {RequisiteField(name) for name in locked.edited_fields}
        current = stored.values.model_dump()
        for field, value in accepted.items():
            if field in edited or field in stored.ambiguous or current[field.value]:
                continue
            current[field.value] = value
            filled += 1
        if filled:
            locked.requisites_ciphertext = encrypt_requisites(
                locked, StoredRequisites(Requisites.model_validate(current), stored.ambiguous)
            )
            await session.execute(
                update(ActRequestFile)
                .where(
                    ActRequestFile.act_request_id == act_request_id,
                    ActRequestFile.file_id.in_(list(file_ids)),
                    ActRequestFile.text_state == FileTextState.IMAGE,
                )
                .values(text_state=FileTextState.VISION)
            )
    logger.info(
        "act_requisites_read_from_scans",
        act_request_id=str(act_request_id),
        files=len(file_ids),
        fields_filled=filled,
        fields_rejected=rejected,
    )
    return filled


def clean_scan_value(field: RequisiteField, raw: str) -> str | None:
    """Значение модели к виду хранения: одна строка, печатные знаки, формат §4.

    None — не тот формат (БИН не из 12 цифр и т. п.) или не сходится контрольная
    сумма БИН/ИИН и ИИК; "" — поле пустое. Контрольная сумма у правки бухгалтера —
    предупреждение (Р-5), а у модели — отказ: прогон 29.10 показал, что модель
    теряет цифру длинного ИИК, а пустое поле лучше догадки — его видно сразу.
    """
    text = " ".join("".join(char for char in raw if char.isprintable() or char.isspace()).split())
    normalized = normalize_field(field, text)
    if normalized is None:
        return None
    value = normalized[:FIELD_MAX_LENGTH]
    return None if field_warnings(Requisites(**{field.value: value})) else value
