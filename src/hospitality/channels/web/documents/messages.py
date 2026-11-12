"""Сообщения гостю в веб-чат о запросе реквизитов для АВР (spec 0049 Р-2, §6).

- «Реквизиты получены» — маршрут формы сразу после приёма (`notify_received`);
- «Готово» — подписчик `request.status_changed` → `done`;
- «Вернули» — `cancelled` сотрудником, с его примечанием;
- «Ещё не готовы» — `done → in_progress`, «Готово» было ошибкой (возврат в работу, #998).

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
  второго сообщения не даст (подсказка страницы «Документы для АВР»); после
  возврата в работу — `…:{stay_id}:{request_id}:{номер возврата}`;
- вернули — `guest:act_returned:{request_id}`;
- ещё не готовы — `guest:act_reopened:{request_id}:{номер возврата}`.

Поправка уходит, только если гостю ушло «Готово» отменённого закрытия и у проживания
нет другого выполненного запроса: «Готово» дубля остаётся правдой. Новое «Готово»
после возврата — только если была поправка или «Готово» проживанию ещё не уходило.
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
from hospitality.channels.web import form_reopen
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
    if form_reopen.is_reopen(event):
        await notify_reopened(event.request_id)
    elif event.new_status is status.DONE:
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
    request = await requests_api.get_request(request_id)
    if returned:
        key: str | None = f"guest:act_returned:{request_id}"
    elif form_reopen.closing_is_late(request, requests_api.RequestStatus.DONE):
        logger.info("act_request_message_skipped_reopened", request_id=str(request_id))
        return
    else:
        key = await _ready_key_to_send(act, request)
    if key is None or await notification_already_sent(key):
        logger.info("act_request_message_skipped_duplicate", request_id=str(request_id))
        return
    note = request.resolution_note
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


async def notify_reopened(request_id: uuid.UUID) -> None:
    """«Ещё не готовы» — к «Готово» закрытия, которое вернули в работу (#998)."""
    act = await documents_api.get_act_request_by_request(request_id)
    if act is None:
        return  # заявка не запрос АВР
    request = await requests_api.get_request(request_id)
    told = request.reopen_count >= 1 and await notification_already_sent(
        _ready_key(act, request_id, request.reopen_count - 1)
    )
    if not told or await _other_request_done(act, request_id):
        logger.info("act_request_message_skipped_not_ready", request_id=str(request_id))
        return
    key = form_reopen.reopen_key(f"guest:act_reopened:{request_id}", request)
    if await notification_already_sent(key):
        logger.info("act_request_message_skipped_duplicate", request_id=str(request_id))
        return
    await _record(act.guest_identity_id, texts.pick(texts.REOPENED, act.guest_language), key)
    logger.info(
        "act_request_message_recorded",
        request_id=str(request_id),
        step="reopened",
        reopen_count=request.reopen_count,
        guest_language=act.guest_language,
    )


def _ready_key(act: documents_api.ActRequestRead, request_id: uuid.UUID, reopen_count: int) -> str:
    """Ключ «Готово» закрытия: первое — на проживание, после возврата — на заявку и номер."""
    stay_key = f"guest:act_ready:{act.stay_id}"
    return f"{stay_key}:{request_id}:{reopen_count}" if reopen_count else stay_key


async def _ready_key_to_send(
    act: documents_api.ActRequestRead, request: requests_api.ServiceRequestRead
) -> str | None:
    """Ключ «Готово» этого закрытия; None — гость уже знает, что готово, и поправки не было."""
    key = _ready_key(act, request.id, request.reopen_count)
    if not request.reopen_count:
        return key
    corrected = await notification_already_sent(
        form_reopen.reopen_key(f"guest:act_reopened:{request.id}", request)
    )
    if corrected or not await notification_already_sent(_ready_key(act, request.id, 0)):
        return key
    return None


async def _other_request_done(act: documents_api.ActRequestRead, request_id: uuid.UUID) -> bool:
    """У проживания есть другой выполненный запрос — его «Готово» гостю остаётся правдой."""
    others = [
        rid for rid in await documents_api.list_stay_request_ids(act.stay_id) if rid != request_id
    ]
    return any(
        other.status is requests_api.RequestStatus.DONE
        for other in await requests_api.list_requests_by_ids(others)
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
