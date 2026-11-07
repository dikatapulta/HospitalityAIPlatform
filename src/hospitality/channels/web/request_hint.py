"""Подсказка веб-гостю после первой заявки: где ждать итог (#223).

Итог заявки («выполнена» / «отменена» с причиной) подписчик пишет в диалог гостя
(`channels/telegram/notifications.py`), но push у веба нет: гость, закрывший
страницу, увидит итог, только вернувшись в чат. Поэтому после первой заявки
диалога — одно статическое сообщение: итог придёт сюда, в этот чат. Про закрытие
страницы и повторный QR-код подсказка молчит (решение основателя 05.11.2026, #857):
возврат по листку работает и так — повторное сканирование с того же телефона
возвращает в этот же диалог (`guests.start_guest_session_by_bind_link`, cookie на
`/w/{slug}`).

Один раз на диалог — ключ `web:request_hint:<conversation_id>` (P-8). Язык — язык
заявки, без модели: ru, kk или en; другой язык — en; язык не определён — все три
строкой каждый, как статические тексты канала до входа. Сбой записи подсказки
ход не роняет: заявка создана, ответ хода уже записан.
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from hospitality.channels.common.store import (
    notification_already_sent,
    record_outbound_message,
)
from hospitality.modules.requests import api as requests_api
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# (ru, kk, en) — первые фразы утверждённых 29.10.2026, остальное снято 05.11.2026
# (docs/founder-review/2026-11-05-pr860-request-hint.md).
HINT: Final = (
    "Итог по заявке придёт сюда, в этот чат.",
    "Өтінімнің нәтижесі осы чатқа келеді.",
    "The outcome of your request will appear here, in this chat.",
)


def hint_text(language: str | None) -> str:
    """Текст на языке заявки; не определён — kk, ru, en подряд (порядок экрана входа)."""
    ru, kk, en = HINT
    if language is None:
        return "\n".join((kk, ru, en))
    return {"ru": ru, "kk": kk}.get(language, en)


async def record_request_hint(
    conversation_id: uuid.UUID, request_id: uuid.UUID, correlation_id: str
) -> tuple[uuid.UUID, str] | None:
    """Записать подсказку, если в диалоге её ещё не было: (id сообщения, текст)."""
    key = f"web:request_hint:{conversation_id}"
    try:
        if await notification_already_sent(key):
            return None
        request = await requests_api.get_request(request_id)
        text = hint_text(request.guest_language)
        message_id = await record_outbound_message(
            conversation_id,
            text,
            correlation_id,
            external_message_id=None,
            idempotency_key=key,
        )
    except IntegrityError:
        # Параллельный ход того же диалога успел первым (UNIQUE ключа).
        logger.info("web_request_hint_raced", request_id=str(request_id))
        return None
    except (SQLAlchemyError, AppError) as error:
        logger.warning(
            "web_request_hint_failed",
            request_id=str(request_id),
            error_type=type(error).__name__,
        )
        return None
    logger.info(
        "web_request_hint_recorded",
        request_id=str(request_id),
        guest_language=request.guest_language,
    )
    return message_id, text
