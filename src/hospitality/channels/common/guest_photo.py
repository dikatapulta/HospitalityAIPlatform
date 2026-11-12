"""Фото гостя к заявке — правило привязки и ответ гостю (spec 0081 §3, §6; issue #82).

Фото принимает и хранит узел файлов (`platform/files.py`), связь с заявкой держит
модуль `requests` (`attach_request_photos`). Здесь — какую заявку выбрать:

- **фото пришло** — свежая открытая заявка диалога (`request_origins`, не старше
  `PHOTO_ATTACH_WINDOW`) получает его, гостю «приложили к заявке #N»; заявки нет —
  фото ждёт текста гостя или его «да» на заявку (гейт P-9);
- **ход создал заявки** — ждущие фото диалога уходят в каждую заявку хода
  (`attach_pending_photos`, шаг `guest_turn` сразу после `record_request_origin`).

Модели на этом пути нет: ответ — статический текст на языке страницы гостя, а в
историю модели фото не входит (`load_dialog_history` берёт только текст). Замка
диалога у фото нет — гонка с ходом модели оставляет фото в переписке (§3).

Запись входящего фото — здесь, а не в `store.py`: тот за границей R-3, а фото —
единственный писатель `messages.file_id`.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Final, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from hospitality.ai.tools import create_service_request
from hospitality.channels.common.models import Message, MessageContentKind, MessageDirection
from hospitality.channels.common.store import (
    load_conversation_request_ids,
    load_pending_action,
    record_outbound_message,
)
from hospitality.modules.requests import api as requests_api
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_guest_photo

logger = get_logger(module=__name__)

# Окно привязки (spec 0081 §3): заявка старше — уже не та, о которой фото; фото
# старше — уже не к заявке, которую гость оформляет сейчас.
PHOTO_ATTACH_WINDOW: Final = timedelta(minutes=60)

PhotoLanguage = Literal["kk", "ru", "en"]


class PhotoOutcome(enum.StrEnum):
    """Что стало с фото при приходе — метка лога и метрики `guest_photos_total`."""

    ATTACHED = "attached"
    PENDING = "pending"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    REQUEST_FULL = "request_full"


@dataclass(frozen=True)
class GuestPhotoReply:
    """Исход фото и ответ гостю, уже записанный исходящим сообщением."""

    outcome: PhotoOutcome
    request_id: uuid.UUID | None
    text: str
    message_id: uuid.UUID


# Тексты ответа (spec 0081 §8, решение — docs/founder-review/2026-11-11-pr996-request-photo.md).
# Пара — «с номером #N» и «без номера» (у заявки нет дневного номера).
_ATTACHED: Final[dict[PhotoLanguage, tuple[str, str]]] = {
    "ru": (
        "Фото приложили к заявке #{n} — служба его увидит.",
        "Фото приложили к вашей заявке — служба его увидит.",
    ),
    "kk": (
        "Фото #{n} өтінімге тіркелді — қызмет оны көреді.",
        "Фото өтініміңізге тіркелді — қызмет оны көреді.",
    ),
    "en": (
        "Photo added to request #{n} — the team will see it.",
        "Photo added to your request — the team will see it.",
    ),
}
_FULL: Final[dict[PhotoLanguage, tuple[str, str]]] = {
    "ru": (
        "К заявке #{n} уже приложено {limit} фото — этого хватит. "
        "Если случилось что-то ещё, напишите.",
        "К вашей заявке уже приложено {limit} фото — этого хватит. "
        "Если случилось что-то ещё, напишите.",
    ),
    "kk": (
        "#{n} өтінімге {limit} фото тіркелген — бұл жеткілікті. Басқа бірдеңе болса, жазыңыз.",
        "Өтініміңізге {limit} фото тіркелген — бұл жеткілікті. Басқа бірдеңе болса, жазыңыз.",
    ),
    "en": (
        "Request #{n} already has {limit} photos — that's enough. "
        "If something else happened, just write.",
        "Your request already has {limit} photos — that's enough. "
        "If something else happened, just write.",
    ),
}
_PENDING: Final[dict[PhotoLanguage, str]] = {
    "ru": "Фото получили. Напишите в двух словах, что случилось, — передадим фото службе "
    "вместе с заявкой.",
    "kk": "Фотоны алдық. Не болғанын қысқаша жазыңыз — фотоны өтініммен бірге қызметке жібереміз.",
    "en": "Got your photo. Tell us in a few words what happened — we'll pass it to the team "
    "with your request.",
}
_AWAITING: Final[dict[PhotoLanguage, str]] = {
    "ru": "Фото получили — приложим его к заявке, как только вы её подтвердите.",
    "kk": "Фотоны алдық — өтінімді растағаннан кейін оған тіркейміз.",
    "en": "Got your photo — we'll add it to the request as soon as you confirm it.",
}


def reply_text(outcome: PhotoOutcome, daily_number: int | None, language: PhotoLanguage) -> str:
    """Ответ гостю на фото — одной строкой на языке его страницы."""
    if outcome is PhotoOutcome.PENDING:
        return _PENDING[language]
    if outcome is PhotoOutcome.AWAITING_CONFIRMATION:
        return _AWAITING[language]
    numbered, plain = (_ATTACHED if outcome is PhotoOutcome.ATTACHED else _FULL)[language]
    template = numbered if daily_number is not None else plain
    return template.format(n=daily_number, limit=requests_api.MAX_REQUEST_PHOTOS)


async def insert_inbound_photo(
    conversation_id: uuid.UUID,
    file_id: uuid.UUID,
    *,
    idempotency_key: str,
    external_message_id: str,
    correlation_id: str,
) -> Message | None:
    """Записать входящее фото гостя; None — этот ключ уже записан (P-8, канон
    `store.insert_inbound_message`): проигравший гонку повтор удаляет свой файл сам."""
    try:
        async with session_scope() as session:
            row = Message(
                conversation_id=conversation_id,
                direction=MessageDirection.INBOUND,
                content_kind=MessageContentKind.PHOTO,
                text=None,
                file_id=file_id,
                external_message_id=external_message_id,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
            )
            session.add(row)
            await session.flush()
    except IntegrityError as error:
        if "uq_messages_tenant_id" not in str(error):
            raise
        return None
    return row


async def find_inbound_photo(conversation_id: uuid.UUID, idempotency_key: str) -> Message | None:
    """Входящее фото диалога, уже записанное этим ключом отправки, — для ответа на повтор."""
    async with session_scope() as session:
        return await session.scalar(
            select(Message).where(
                Message.conversation_id == conversation_id,
                Message.idempotency_key == idempotency_key,
                Message.content_kind == MessageContentKind.PHOTO,
            )
        )


async def is_conversation_photo(conversation_id: uuid.UUID, file_id: uuid.UUID) -> bool:
    """Файл — входящее фото этого диалога: только такое гость получает обратно (§4)."""
    async with session_scope() as session:
        found = await session.scalar(
            select(Message.id).where(
                Message.conversation_id == conversation_id,
                Message.direction == MessageDirection.INBOUND,
                Message.content_kind == MessageContentKind.PHOTO,
                Message.file_id == file_id,
            )
        )
    return found is not None


async def receive_guest_photo(
    conversation_id: uuid.UUID,
    file_id: uuid.UUID,
    *,
    language: PhotoLanguage,
    correlation_id: str,
) -> GuestPhotoReply:
    """Правило §3 при приходе фото и ответ гостю исходящим сообщением.

    Входящее фото уже записано (`insert_inbound_photo`). Ответ пишется в историю —
    модель видит его своей репликой и знает, что фото пришло (§4).
    """
    outcome, request = await _place_photo(conversation_id, file_id)
    text = reply_text(outcome, request.daily_number if request else None, language)
    message_id = await record_outbound_message(
        conversation_id, text, correlation_id, external_message_id=None
    )
    request_id = request.id if request else None
    logger.info(
        "guest_photo_received",
        outcome=outcome.value,
        request_id=str(request_id) if request_id else None,
    )
    record_guest_photo(outcome.value)
    return GuestPhotoReply(outcome=outcome, request_id=request_id, text=text, message_id=message_id)


async def _place_photo(
    conversation_id: uuid.UUID, file_id: uuid.UUID
) -> tuple[PhotoOutcome, requests_api.ServiceRequestRead | None]:
    since = utc_now() - PHOTO_ATTACH_WINDOW
    open_requests = await requests_api.list_open_requests_by_ids(
        await load_conversation_request_ids(conversation_id)
    )
    fresh = [request for request in open_requests if request.created_at >= since]
    if fresh:
        # Порядок — по времени создания: последняя — самая свежая заявка гостя.
        target = fresh[-1]
        attached = await requests_api.attach_request_photos(target.id, [file_id])
        if attached.attached_file_ids:
            return PhotoOutcome.ATTACHED, target
        if attached.photo_count >= requests_api.MAX_REQUEST_PHOTOS:
            return PhotoOutcome.REQUEST_FULL, target
        # Заявку закрыли между чтением и приложением — как будто её нет.
    pending = await load_pending_action(conversation_id)
    if pending is not None and pending.get("tool_name") == create_service_request.NAME:
        return PhotoOutcome.AWAITING_CONFIRMATION, None
    return PhotoOutcome.PENDING, None


async def attach_pending_photos(
    conversation_id: uuid.UUID, request_ids: Sequence[uuid.UUID]
) -> None:
    """Ждущие фото диалога — в каждую заявку хода, старшие первыми (§3).

    Ждущее — входящее фото моложе `PHOTO_ATTACH_WINDOW`, не приложенное ни к одной
    заявке. Несколько заявок хода получают одно и то же фото: исполнитель нужной не
    останется без него. Best-effort: сбой — WARNING, ход и реплика гостя не страдают.
    """
    if not request_ids:
        return
    try:
        pending = await _pending_photo_file_ids(conversation_id)
        if not pending:
            return
        attached = 0
        for request_id in request_ids:
            result = await requests_api.attach_request_photos(request_id, pending)
            attached += len(result.attached_file_ids)
    except Exception:
        logger.warning("guest_photo_attach_failed", exc_info=True)
        return
    logger.info(
        "guest_photos_attached",
        requests=len(request_ids),
        photos=len(pending),
        attached=attached,
    )


async def _pending_photo_file_ids(conversation_id: uuid.UUID) -> list[uuid.UUID]:
    since = utc_now() - PHOTO_ATTACH_WINDOW
    async with session_scope() as session:
        rows = await session.scalars(
            select(Message.file_id)
            .where(
                Message.conversation_id == conversation_id,
                Message.direction == MessageDirection.INBOUND,
                Message.content_kind == MessageContentKind.PHOTO,
                Message.file_id.is_not(None),
                Message.created_at >= since,
            )
            .order_by(Message.created_at, Message.id)
        )
        file_ids = [file_id for file_id in rows if file_id is not None]
    if not file_ids:
        return []
    already = await requests_api.list_attached_file_ids(file_ids)
    return [file_id for file_id in file_ids if file_id not in already]
