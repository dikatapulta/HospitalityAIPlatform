"""Фото гостя к заявке (spec 0081 §5, issue #82).

Файл принимает и хранит узел ядра `platform/files.py` (назначение
`request_photo`): проверка в песочнице, WebP не больше 960 px без EXIF,
шифрование, срок. Модуль держит только связь заявки с файлом
(`request_photos`) и решает, к какой заявке фото приложено и кому его отдать.
Какую заявку выбрать, решает вызывающий (правило §3 — канал гостя); здесь —
предел, статус и идемпотентность.

Событий нет: кабинет опрашивает ленту сам (spec 0039 §3). Отдельный файл, а не
функции в `service.py`: тот за границей R-3 — довод `payment.py` и `sent.py`.
Вход снаружи — реэкспорт через `api.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from typing import Final

from sqlalchemy import select

from hospitality.modules.requests.models import RequestPhoto, ServiceRequest
from hospitality.modules.requests.schemas import RequestPhotosAttached
from hospitality.modules.requests.service import _OPEN_STATUSES
from hospitality.platform.files import FilePurpose, StoredFile, UploadedFile, read_file
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_REQUESTS_PHOTO_NOT_FOUND = "ERR-REQUESTS-010"

# Предел фото у одной заявки (spec 0081 §3): исполнителю хватит, лента не пухнет.
MAX_REQUEST_PHOTOS: Final = 5


async def attach_request_photos(
    request_id: uuid.UUID, file_ids: Sequence[uuid.UUID]
) -> RequestPhotosAttached:
    """Приложить фото к открытой заявке, по порядку, пока не наберётся пять.

    Под блокировкой строки заявки (`FOR UPDATE`): два приложения разом не
    перескочат предел. Уже приложенное — пропуск (P-8). Заявка закрыта, чужая
    или её нет — пустой итог, не ошибка: гость не виноват, что заявку закрыли
    между фото и ходом. Файл, который не фото к заявке этого тенанта, — пропуск:
    внешний ключ проверяется в обход RLS, и чужой `file_id` без этой сверки лёг бы
    в связь.
    """
    wanted = list(dict.fromkeys(file_ids))
    async with session_scope() as session:
        locked = await session.scalar(
            select(ServiceRequest.id)
            .where(ServiceRequest.id == request_id, ServiceRequest.status.in_(_OPEN_STATUSES))
            .with_for_update()
        )
        if locked is None:
            logger.info("request_photos_attach_skipped", request_id=str(request_id))
            return RequestPhotosAttached(attached_file_ids=[], photo_count=0)
        existing = set(
            await session.scalars(
                select(RequestPhoto.file_id).where(RequestPhoto.request_id == request_id)
            )
        )
        own_photos = (
            set(
                await session.scalars(
                    select(UploadedFile.id).where(
                        UploadedFile.id.in_(wanted),
                        UploadedFile.purpose == FilePurpose.REQUEST_PHOTO.value,
                    )
                )
            )
            if wanted
            else set()
        )
        room = max(MAX_REQUEST_PHOTOS - len(existing), 0)
        attached = [
            file_id for file_id in wanted if file_id in own_photos and file_id not in existing
        ][:room]
        session.add_all(RequestPhoto(request_id=request_id, file_id=fid) for fid in attached)
        await session.flush()
    photo_count = len(existing) + len(attached)
    logger.info(
        "request_photos_attached",
        request_id=str(request_id),
        attached=len(attached),
        photo_count=photo_count,
        rejected=len(wanted) - len(own_photos),
    )
    return RequestPhotosAttached(attached_file_ids=attached, photo_count=photo_count)


async def list_request_photo_ids(
    request_ids: Collection[uuid.UUID],
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Фото заявок одним запросом — для страницы ленты; порядок — как прикладывали.

    Заявки без фото в ответ не попадают.
    """
    if not request_ids:
        return {}
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(RequestPhoto.request_id, RequestPhoto.file_id)
                .where(RequestPhoto.request_id.in_(request_ids))
                .order_by(RequestPhoto.created_at, RequestPhoto.id)
            )
        ).all()
    photos: dict[uuid.UUID, list[uuid.UUID]] = {}
    for request_id, file_id in rows:
        photos.setdefault(request_id, []).append(file_id)
    return photos


async def list_attached_file_ids(file_ids: Collection[uuid.UUID]) -> set[uuid.UUID]:
    """Какие из этих файлов уже приложены хоть к одной заявке (ждущие фото — нет)."""
    if not file_ids:
        return set()
    async with session_scope() as session:
        return set(
            await session.scalars(
                select(RequestPhoto.file_id).where(RequestPhoto.file_id.in_(file_ids)).distinct()
            )
        )


async def read_request_photo(
    request_id: uuid.UUID,
    file_id: uuid.UUID,
    *,
    allowed_category_ids: Collection[uuid.UUID] | None = None,
) -> StoredFile:
    """Фото этой заявки для выдачи; связи нет — ERR-REQUESTS-010 (404).

    `allowed_category_ids` — службы смотрящего (spec 0045 §5): фото заявки другой
    службы — тот же 404, что и несуществующее, — по нему не узнать, что заявка есть.
    `None` — любые службы (ресепшен, менеджер). Сбои хранилища — коды узла
    (ERR-FILES-005, -006).
    """
    query = (
        select(RequestPhoto.id)
        .join(ServiceRequest, ServiceRequest.id == RequestPhoto.request_id)
        .where(RequestPhoto.request_id == request_id, RequestPhoto.file_id == file_id)
    )
    if allowed_category_ids is not None:
        query = query.where(ServiceRequest.category_id.in_(allowed_category_ids))
    async with session_scope() as session:
        linked = await session.scalar(query)
    if linked is None:
        logger.info("request_photo_not_found", request_id=str(request_id), file_id=str(file_id))
        raise AppError(
            code=ERR_REQUESTS_PHOTO_NOT_FOUND, message="Фото не найдено.", status_code=404
        )
    return await read_file(file_id)
