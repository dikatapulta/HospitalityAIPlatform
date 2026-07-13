"""Команды персонала в staff-чате (Task 0017, ADR-011).

Заглушка кабинета персонала (Phase 1) для walking skeleton: сотрудник двигает
заявку по жизненному циклу командами в чате `TELEGRAM_STAFF_CHAT_ID`. Одна команда
на переход `STATUS_TRANSITIONS` модуля requests — карта переходов не обходится:

    /assign <id>   new → assigned
    /start  <id>   assigned → in_progress
    /done   <id>   in_progress → done
    /cancel <id>   * → cancelled

Обработчик зовёт публичный сервис `requests.change_request_status` (P-5: то же
действие доступно и через будущий кабинет/API) и отвечает персоналу результатом.
Подтверждение гостю при `done` идёт НЕ отсюда, а подписчиком `request.status_changed`
(`notifications.py`, P-6): команда лишь публикует событие. RBAC нет (любой в
staff-чате закрывает заявки) — приемлемо для одного демо-чата Phase 0 (§17.7).
"""

from __future__ import annotations

import uuid

from hospitality.channels.base import MessageKind, NormalizedMessage
from hospitality.channels.telegram.client import TelegramSender
from hospitality.channels.telegram.outbound import send_reply
from hospitality.modules.requests import api as requests_api
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Команда (verb без «/») → целевой статус перехода.
_STATUS_BY_VERB: dict[str, requests_api.RequestStatus] = {
    "assign": requests_api.RequestStatus.ASSIGNED,
    "start": requests_api.RequestStatus.IN_PROGRESS,
    "done": requests_api.RequestStatus.DONE,
    "cancel": requests_api.RequestStatus.CANCELLED,
}

_HELP = (
    "Команды службы: /assign <id> · /start <id> · /done <id> · /cancel <id>. "
    "id заявки — из уведомления о ней."
)

# Понятная персоналу расшифровка ожидаемых ошибок сервиса (R-8, каталог errors.md).
_ERROR_HINTS = {
    requests_api.ERR_REQUESTS_REQUEST_NOT_FOUND: "Заявка не найдена.",
    requests_api.ERR_REQUESTS_INVALID_STATUS_TRANSITION: (
        "Недопустимый переход — заявка уже в другом состоянии."
    ),
}


async def handle_staff_message(
    conversation_id: uuid.UUID,
    normalized: NormalizedMessage,
    *,
    sender: TelegramSender,
    correlation_id: str,
) -> None:
    """Обработать сообщение из staff-чата как команду (внутри `tenant_context`)."""
    if normalized.kind is not MessageKind.TEXT or normalized.text is None:
        reply = _HELP
    else:
        reply = await _run_command(normalized.text)
    await send_reply(
        conversation_id, normalized.chat_id, reply, sender=sender, correlation_id=correlation_id
    )


async def _run_command(text: str) -> str:
    """Разобрать и исполнить команду; вернуть текст ответа персоналу."""
    parts = text.strip().split()
    if not parts:
        return _HELP
    # В группах Telegram дописывает @botusername к команде — отбрасываем.
    verb = parts[0].split("@", 1)[0].lstrip("/").lower()
    target = _STATUS_BY_VERB.get(verb)
    if target is None:
        return _HELP
    if len(parts) < 2:
        return f"Укажите id заявки: /{verb} <request_id>."
    try:
        request_id = uuid.UUID(parts[1])
    except ValueError:
        return f"Не разобрал id заявки «{parts[1]}» — ожидается UUID из уведомления."

    try:
        updated = await requests_api.change_request_status(request_id, target)
    except AppError as error:
        logger.info("staff_command_rejected", verb=verb, error_code=error.code)
        hint = _ERROR_HINTS.get(error.code, error.message)
        return f"Не получилось ({error.code}): {hint}"

    logger.info("staff_command_applied", verb=verb, request_id=str(request_id))
    return f"Заявка {str(request_id)[:8]} «{updated.summary}» → {updated.status.value}."
