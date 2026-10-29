"""Сообщения гостю в веб-чат о запросе реквизитов для АВР (spec 0049 Р-2, §6).

- «Реквизиты получены» — маршрут формы сразу после приёма (`notify_received`);
- «Готово» — подписчик `request.status_changed` → `done`;
- «Вернули» — `cancelled` сотрудником, с его примечанием.

Свой подписчик, а не общий: привязки `request_origins` у запроса нет (Р-2) —
общий путь закрытия перевёл бы текст моделью, а AI увидел бы запрос в «моих
заявках». Заявка не запрос АВР — подписчик молчит. Отмена гостем в чат не
пишется (гость запрос АВР не отменяет, но проверка дешевле догадки).

Адрес — веб-диалог `guest_identity_id` запроса (`channels/web/service.py` ведёт
один диалог на идентичность); сообщение — запись исходящего `Message`, страница
чата заберёт его опросом. Текст — статический, на языке запроса; примечание
бухгалтерии — как написано. Ключи идемпотентности (P-8):

- получено — `guest:act_received:{act_request_id}`: повтор формы тем же ключом;
- готово — `guest:act_ready:{stay_id}`: дубль запроса, закрытый «Готово»,
  второго сообщения не даст (подсказка страницы «Документы для АВР»);
- вернули — `guest:act_returned:{request_id}`.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.exc import IntegrityError

from hospitality.channels.common.store import (
    ensure_conversation,
    notification_already_sent,
    record_outbound_message,
)
from hospitality.channels.web.documents import texts
from hospitality.channels.web.documents.hotel import hotel_zone
from hospitality.channels.web.service import CHANNEL
from hospitality.modules.documents import api as documents_api
from hospitality.modules.requests import api as requests_api
from hospitality.shared.events import subscribe
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


def register() -> None:
    """Подключить подписчика к событиям заявки — зовёт `hospitality/worker.py`."""
    subscribe(requests_api.RequestStatusChanged, on_request_status_changed)


async def on_request_status_changed(event: requests_api.RequestStatusChanged) -> None:
    status = requests_api.RequestStatus
    if event.new_status is status.DONE:
        await notify_closed(event.request_id, returned=False)
    elif event.new_status is status.CANCELLED:
        if event.initiator is requests_api.RequestInitiator.GUEST:
            return
        await notify_closed(event.request_id, returned=True)


async def notify_closed(request_id: uuid.UUID, *, returned: bool) -> None:
    """«Готово» (одно на проживание) или «Вернули» (одно на запрос)."""
    act = await documents_api.get_act_request_by_request(request_id)
    if act is None:
        return  # заявка не запрос АВР
    key = f"guest:act_returned:{request_id}" if returned else f"guest:act_ready:{act.stay_id}"
    if await notification_already_sent(key):
        logger.info("act_request_message_skipped_duplicate", request_id=str(request_id))
        return
    note = (await requests_api.get_request(request_id)).resolution_note
    text = (
        compose_returned(act.guest_language, note)
        if returned
        else compose_ready(act.guest_language, note)
    )
    await _record(act.guest_identity_id, text, key)
    logger.info(
        "act_request_message_recorded",
        request_id=str(request_id),
        step="returned" if returned else "ready",
        with_note=note is not None,
        guest_language=act.guest_language,
    )


async def notify_received(act: documents_api.ActRequestRead) -> None:
    """«Реквизиты получены» — пишет маршрут формы; повтор формы второго не даст."""
    key = f"guest:act_received:{act.id}"
    if await notification_already_sent(key):
        return
    date = texts.day_label(act.check_out_at, await hotel_zone(), act.guest_language)
    await _record(
        act.guest_identity_id, texts.pick(texts.RECEIVED, act.guest_language).format(date=date), key
    )


def compose_ready(language: str, note: str | None) -> str:
    text = texts.pick(texts.READY, language)
    if note:
        text += "\n" + texts.pick(texts.READY_NOTE, language).format(note=note)
    return text


def compose_returned(language: str, note: str | None) -> str:
    return texts.pick(texts.RETURNED, language).format(note=f": {note}" if note else "")


async def _record(identity: uuid.UUID, text: str, key: str) -> None:
    conversation_id = await ensure_conversation(CHANNEL, str(identity), guest_identity_id=identity)
    try:
        await record_outbound_message(
            conversation_id, text, _correlation_id(), external_message_id=None, idempotency_key=key
        )
    except IntegrityError:
        # Параллельная доставка того же шага успела первой (UNIQUE ключа) — второго нет.
        logger.info("act_request_message_raced", idempotency_key=key)


def _correlation_id() -> str:
    """correlation_id запроса или события — его восстановил доставщик outbox (§10.2)."""
    value = structlog.contextvars.get_contextvars().get("correlation_id")
    return value if isinstance(value, str) else ""
