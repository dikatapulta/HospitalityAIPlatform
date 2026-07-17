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
    "Команды службы: /assign <#N> · /start <#N> · /done <#N> · /cancel <#N>. "
    "Номер заявки #N — из уведомления о ней (принимается и полный id)."
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
    """Обработать сообщение из staff-чата как команду (внутри `tenant_context`).

    Бот реагирует ТОЛЬКО на команды — текст с ведущим «/». Обычная переписка
    персонала (и не-текст: фото/голос) остаётся без ответа: иначе бот отвечает
    подсказкой на каждое сообщение живой группы, её мьютят, и вместе со спамом
    теряются уведомления о заявках (S-2, #38 п.4).
    """
    if normalized.kind is not MessageKind.TEXT or normalized.text is None:
        return
    if not normalized.text.lstrip().startswith("/"):
        return
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
        return f"Укажите номер заявки: /{verb} <#N>."
    resolved = await _resolve_request(parts[1], verb)
    if isinstance(resolved, str):
        return resolved  # готовый ответ персоналу: не найдено / неоднозначно / кривой ввод
    request_id = resolved

    try:
        updated = await requests_api.change_request_status(request_id, target)
    except AppError as error:
        logger.info("staff_command_rejected", verb=verb, error_code=error.code)
        hint = _ERROR_HINTS.get(error.code, error.message)
        return f"Не получилось ({error.code}): {hint}"

    label = f"#{updated.daily_number}" if updated.daily_number is not None else str(request_id)[:8]
    logger.info("staff_command_applied", verb=verb, request_id=str(request_id))
    return f"Заявка {label} «{updated.summary}» → {updated.status.value}."


async def _resolve_request(raw: str, verb: str) -> uuid.UUID | str:
    """Разобрать аргумент команды в id заявки — по дневному номеру `#N` или UUID.

    Возвращает `uuid.UUID` (заявка найдена однозначно) либо готовый текст ответа
    персоналу: заявка не найдена, номер неоднозначен (несколько незакрытых с этим
    `#N` — просим уточнить полным id), или ввод не разобран. Ведущий `#` в номере
    допускается (`/done #12`).
    """
    token = raw.lstrip("#")
    if token.isdigit():
        return await _resolve_by_daily_number(int(token), verb)
    try:
        return uuid.UUID(raw)
    except ValueError:
        return f"Не разобрал «{raw}» — укажите номер заявки #N из уведомления."


async def _resolve_by_daily_number(number: int, verb: str) -> uuid.UUID | str:
    """Найти незакрытую заявку тенанта по дневному номеру `#N`.

    Одна — её id; ни одной — сообщение; несколько (номер за сутки повторился) —
    просим уточнить полным id по списку кандидатов (issue #38: номер — метка,
    не ключ, поэтому неоднозначность разрешает человек).
    """
    matches = await requests_api.find_open_requests_by_daily_number(number)
    if not matches:
        return f"Заявка #{number} среди незакрытых не найдена."
    if len(matches) > 1:
        options = "\n".join(f"• {_describe(match)} → /{verb} {match.id}" for match in matches)
        return f"Несколько незакрытых заявок #{number} — уточните полным id:\n{options}"
    return matches[0].id


def _describe(request: requests_api.ServiceRequestRead) -> str:
    """Короткая опознавалка заявки для списка неоднозначности: комната + суть."""
    room = f"комн. {request.room_number}, " if request.room_number else ""
    return f"{room}«{request.summary}»"
