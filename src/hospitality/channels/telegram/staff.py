"""Команды и кнопки персонала в staff-чате (Task 0017, ADR-011, spec 0021 П-2/П-4).

Заглушка кабинета персонала (Phase 1) для walking skeleton: сотрудник двигает
заявку по жизненному циклу в чате `TELEGRAM_STAFF_CHAT_ID`. Все пути — кнопки,
команды, реплаи — зовут один и тот же `requests.change_request_status`: карта
переходов `STATUS_TRANSITIONS` не обходится (P-5, DoD #38).

    /start  <#N>          new → in_progress («взять в работу»)
    /done   <#N> [текст]   in_progress → done (+примечание «что не сделано»)
    /cancel <#N> [текст]   * → cancelled (+причина)

`/assign` упразднён вместе со статусом assigned (ADR-013): на эту команду бот
отвечает подсказкой «сразу /start» — переучивание, не молчание.

Ноль ручного ввода (issue #38 п.2–3):
- inline-кнопки под уведомлением о заявке (`keyboards.py`) — нажатие исполняет
  переход, бот отвечает тостом и перерисовывает кнопки под новый статус;
- команда ответом (reply) на уведомление — заявка резолвится по
  `external_message_id` уведомления (обратный поиск по ключам `messages`);
- «⚠️ Готово частично» — бот задаёт вопрос (ForceReply), ответ-реплай персонала
  становится примечанием `resolution_note` и закрывает заявку (spec 0021 П-4).

Подтверждение гостю идёт НЕ отсюда, а подписчиком `request.status_changed`
(`notifications.py`, P-6). RBAC нет (любой в staff-чате двигает заявки) —
приемлемо для одного демо-чата Phase 0 (§17.7); «кто сделал» пишется в логи
(`actor_external_id`).
"""

from __future__ import annotations

import uuid

from hospitality.channels.base import MessageKind, NormalizedMessage
from hospitality.channels.telegram import keyboards
from hospitality.channels.telegram.client import TelegramSender
from hospitality.channels.telegram.outbound import send_reply
from hospitality.channels.telegram.store import (
    load_request_id_for_staff_message,
    load_staff_notification_message_id,
    record_outbound_message,
)
from hospitality.modules.requests import api as requests_api
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Команда (verb без «/») → целевой статус перехода.
_STATUS_BY_VERB: dict[str, requests_api.RequestStatus] = {
    "start": requests_api.RequestStatus.IN_PROGRESS,
    "done": requests_api.RequestStatus.DONE,
    "cancel": requests_api.RequestStatus.CANCELLED,
}

# Команды, у которых хвост после номера — примечание закрытия (spec 0021 П-4).
_VERBS_WITH_NOTE = frozenset({"done", "cancel"})

_HELP = (
    "Команды службы: /start <#N> (взять в работу) · /done <#N> [что не сделано] · "
    "/cancel <#N> [причина]. Номер #N — из уведомления (принимается и полный id); "
    "команду можно отправить ответом на уведомление — тогда номер не нужен."
)

# Ответ на упразднённый /assign (ADR-013): персонал недели пользовался старой
# схемой — молчание выглядело бы поломкой, подсказка переучивает.
_ASSIGN_RETIRED = "Шаг /assign упразднён — сразу берите в работу: /start <#N>."

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
    """Обработать входящее из staff-чата (внутри `tenant_context`).

    Бот реагирует ТОЛЬКО на команды («/…»), нажатия кнопок и ответы-реплаи на
    свой вопрос о примечании. Обычная переписка персонала (и не-текст) остаётся
    без ответа: иначе бот спамит живую группу, её мьютят, и вместе со спамом
    теряются уведомления о заявках (S-2, #38 п.4).
    """
    if normalized.kind is MessageKind.CALLBACK:
        await _handle_callback(
            conversation_id, normalized, sender=sender, correlation_id=correlation_id
        )
        return
    if normalized.kind is not MessageKind.TEXT or normalized.text is None:
        return
    if normalized.text.lstrip().startswith("/"):
        reply = await _run_command(normalized, sender=sender)
        await send_reply(
            conversation_id, normalized.chat_id, reply, sender=sender, correlation_id=correlation_id
        )
        return
    # Обычный текст: единственный смысл — ответ-реплай на наш вопрос «что не
    # сделано?» (ForceReply, spec 0021 П-4). Всё остальное — молчание (S-2).
    note_target = await _note_prompt_target(normalized)
    if note_target is None:
        return
    _, reply = await _apply_transition(
        note_target,
        requests_api.RequestStatus.DONE,
        resolution_note=normalized.text,
        normalized=normalized,
        sender=sender,
    )
    await send_reply(
        conversation_id, normalized.chat_id, reply, sender=sender, correlation_id=correlation_id
    )


async def _handle_callback(
    conversation_id: uuid.UUID,
    normalized: NormalizedMessage,
    *,
    sender: TelegramSender,
    correlation_id: str,
) -> None:
    """Нажатие inline-кнопки: переход или вопрос о примечании (spec 0021 П-2/П-4).

    Ошибки отвечаются ТОСТОМ (answerCallbackQuery), а не сообщением в группу:
    устаревшая кнопка не должна спамить чат. Успешный переход — тост + строка
    в чат (группа видит ход заявки) + перерисовка кнопок под новый статус.
    """
    parsed = keyboards.parse_callback_data(normalized.text or "")
    if parsed is None:
        await _toast(
            sender, normalized, "Кнопка устарела — используйте команды: /start /done /cancel."
        )
        return
    request_id, action = parsed
    logger.info(
        "staff_callback_received",
        action=action.value,
        request_id=str(request_id),
        actor=normalized.actor_external_id,
    )

    if action is keyboards.CallbackAction.DONE_NOTE:
        await _ask_resolution_note(
            conversation_id, request_id, normalized, sender=sender, correlation_id=correlation_id
        )
        return

    applied, reply = await _apply_transition(
        request_id,
        keyboards.STATUS_BY_ACTION[action],
        resolution_note=None,
        normalized=normalized,
        sender=sender,
        toast=True,
    )
    if not applied:
        return  # ошибка уже отвечена тостом — не спамим группу (S-2)
    await send_reply(
        conversation_id, normalized.chat_id, reply, sender=sender, correlation_id=correlation_id
    )


async def _ask_resolution_note(
    conversation_id: uuid.UUID,
    request_id: uuid.UUID,
    normalized: NormalizedMessage,
    *,
    sender: TelegramSender,
    correlation_id: str,
) -> None:
    """«Готово частично» → вопрос персоналу с ForceReply (spec 0021 П-4).

    Ответ-реплай на этот вопрос закрывает заявку с примечанием (`_note_prompt_target`
    находит её по ключу `staff:note_prompt:<id>:…`). Ожидание нигде не персистится:
    не ответили — заявка осталась in_progress, кнопка задаёт вопрос снова.
    """
    try:
        request = await requests_api.get_request(request_id)
    except AppError as error:
        await _toast(sender, normalized, _ERROR_HINTS.get(error.code, error.message))
        return
    label = _label(request)
    text = f"Заявка {label}: что не сделано и почему? Ответьте на это сообщение."
    try:
        sent_id = await sender.send_message(
            normalized.chat_id, text, reply_markup={"force_reply": True}
        )
    except Exception as error:  # best-effort, как send_reply: вебхук не роняем
        logger.warning("telegram_send_failed", chat_id=normalized.chat_id, error=str(error))
        return
    if sent_id is None:
        # Без message_id вопроса ответ-реплай персонала не с чем связать (обратный
        # поиск идёт по external_message_id) — не пишем непривязываемый prompt и
        # просим повторить, иначе ответ потерялся бы молча.
        logger.warning("staff_note_prompt_no_message_id", request_id=str(request_id))
        await _toast(sender, normalized, "Не смог задать вопрос — повторите нажатие.")
        return
    # Ключ несёт id заявки — по нему ответ-реплай найдёт её (обратный поиск).
    # Суффикс callback_id делает ключ уникальным на каждое нажатие кнопки.
    await record_outbound_message(
        conversation_id,
        text,
        correlation_id,
        external_message_id=sent_id,
        idempotency_key=f"staff:note_prompt:{request_id}:{normalized.callback_id}",
    )
    await _toast(sender, normalized, f"Жду примечание к {label} ответом на вопрос.")


async def _run_command(normalized: NormalizedMessage, *, sender: TelegramSender) -> str:
    """Разобрать и исполнить команду; вернуть текст ответа персоналу."""
    text = normalized.text or ""
    parts = text.strip().split()
    if not parts:
        return _HELP
    # В группах Telegram дописывает @botusername к команде — отбрасываем.
    verb = parts[0].split("@", 1)[0].lstrip("/").lower()
    if verb == "assign":
        return _ASSIGN_RETIRED
    target = _STATUS_BY_VERB.get(verb)
    if target is None:
        return _HELP

    resolved = await _resolve_target(parts, normalized, verb)
    if isinstance(resolved, str):
        return resolved  # готовый ответ персоналу: не найдено / неоднозначно / кривой ввод
    request_id, note = resolved
    if verb not in _VERBS_WITH_NOTE:
        note = None  # /start примечания не несёт
    _, reply = await _apply_transition(
        request_id, target, resolution_note=note, normalized=normalized, sender=sender
    )
    return reply


async def _resolve_target(
    parts: list[str], normalized: NormalizedMessage, verb: str
) -> tuple[uuid.UUID, str | None] | str:
    """Найти заявку команды: явный аргумент (#N/UUID) или реплай на уведомление.

    Возвращает (id, примечание-хвост) либо готовый текст ответа персоналу.
    Приоритет у явного аргумента; при команде-реплае весь хвост после глагола —
    примечание (`/done кофе не принесли` ответом на уведомление, #38 п.3).
    """
    explicit = parts[1] if len(parts) > 1 else None
    if explicit is not None and (explicit.lstrip("#").isdigit() or _is_uuid(explicit)):
        resolved = await _resolve_request(explicit, verb)
        if isinstance(resolved, str):
            return resolved
        return resolved, _join_note(parts[2:])

    replied = await _replied_request_id(normalized)
    if replied is not None:
        return replied, _join_note(parts[1:])

    if explicit is None:
        return (
            f"Укажите номер заявки: /{verb} <#N> — "
            "или отправьте команду ответом на уведомление о заявке."
        )
    return (
        f"Не разобрал «{explicit}» — укажите номер #N из уведомления "
        "или отправьте команду ответом на само уведомление."
    )


async def _apply_transition(
    request_id: uuid.UUID,
    target: requests_api.RequestStatus,
    *,
    resolution_note: str | None,
    normalized: NormalizedMessage,
    sender: TelegramSender,
    toast: bool = False,
) -> tuple[bool, str]:
    """Единая точка перехода для команд, кнопок и примечаний-реплаев (P-5).

    Возвращает (применён ли переход, текст-итог). Успех: обновить кнопки
    уведомления под новый статус (best-effort) + строка-итог для чата. Ошибка:
    понятный текст; при нажатой кнопке — ещё и тост (в группу его не шлют —
    устаревшая кнопка не должна спамить чат, S-2).
    """
    try:
        updated = await requests_api.change_request_status(
            request_id, target, resolution_note=resolution_note
        )
    except AppError as error:
        logger.info("staff_command_rejected", target=target.value, error_code=error.code)
        hint = _ERROR_HINTS.get(error.code, error.message)
        if toast:
            await _toast(sender, normalized, hint)
        return False, f"Не получилось ({error.code}): {hint}"

    logger.info(
        "staff_command_applied",
        target=target.value,
        request_id=str(request_id),
        actor=normalized.actor_external_id,
        with_note=resolution_note is not None,
    )
    if toast:
        await _toast(sender, normalized, f"Заявка {_label(updated)} → {updated.status.value}.")
    await _refresh_notification_keyboard(updated, normalized, sender)
    result = f"Заявка {_label(updated)} «{updated.summary}» → {updated.status.value}."
    if updated.resolution_note:
        result += f"\nПримечание: {updated.resolution_note}"
    return True, result


async def _refresh_notification_keyboard(
    request: requests_api.ServiceRequestRead,
    normalized: NormalizedMessage,
    sender: TelegramSender,
) -> None:
    """Перерисовать кнопки под уведомлением о заявке после перехода (best-effort).

    Откуда message_id: у нажатой кнопки — из `reply_to` (сообщение с кнопками);
    у текстовой команды — обратным поиском по ключу уведомления. Сбой Bot API
    не мешает переходу: устаревшие кнопки гасит карта переходов + тост.
    """
    message_id: str | None = None
    if normalized.kind is MessageKind.CALLBACK and normalized.reply_to is not None:
        message_id = normalized.reply_to.external_message_id
    else:
        message_id = await load_staff_notification_message_id(request.id)
    if message_id is None:
        return
    markup = keyboards.keyboard_for_status(request.id, request.status)
    try:
        await sender.edit_message_reply_markup(normalized.chat_id, message_id, markup)
    except Exception as error:  # best-effort: кнопки — удобство, не инвариант
        logger.info("staff_keyboard_refresh_failed", error=str(error))


async def _note_prompt_target(normalized: NormalizedMessage) -> uuid.UUID | None:
    """Заявка, к которой относится ответ-реплай на вопрос «что не сделано?».

    None — сообщение не является ответом на наш вопрос о примечании (обычная
    переписка группы, бот молчит). Реплай на само уведомление без команды —
    тоже молчание: намерение неочевидно, для перехода есть кнопки и команды.
    """
    if normalized.reply_to is None:
        return None
    replied_text = normalized.reply_to.text or ""
    if "Ответьте на это сообщение" not in replied_text:
        return None
    return await load_request_id_for_staff_message(normalized.reply_to.external_message_id)


async def _replied_request_id(normalized: NormalizedMessage) -> uuid.UUID | None:
    """Заявка из reply-контекста команды (#38 п.3): уведомление или вопрос бота."""
    if normalized.reply_to is None:
        return None
    return await load_request_id_for_staff_message(normalized.reply_to.external_message_id)


async def _resolve_request(raw: str, verb: str) -> uuid.UUID | str:
    """Разобрать явный аргумент команды — дневной номер `#N` или UUID.

    Возвращает `uuid.UUID` (заявка найдена однозначно) либо готовый текст ответа
    персоналу: заявка не найдена или номер неоднозначен (несколько незакрытых с
    этим `#N` — просим уточнить полным id). Ведущий `#` допускается (`/done #12`).
    """
    token = raw.lstrip("#")
    if token.isdigit():
        return await _resolve_by_daily_number(int(token), verb)
    return uuid.UUID(raw)  # вызывающая сторона уже проверила _is_uuid


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


async def _toast(sender: TelegramSender, normalized: NormalizedMessage, text: str) -> None:
    """Ответ нажавшему кнопку (best-effort): без него Telegram крутит «часики»."""
    if normalized.callback_id is None:
        return
    try:
        await sender.answer_callback_query(normalized.callback_id, text)
    except Exception as error:  # best-effort: тост — удобство, не инвариант
        logger.info("staff_toast_failed", error=str(error))


def _label(request: requests_api.ServiceRequestRead) -> str:
    """Короткое имя заявки для ответов персоналу: #N, фолбэк — префикс id."""
    if request.daily_number is not None:
        return f"#{request.daily_number}"
    return str(request.id)[:8]


def _describe(request: requests_api.ServiceRequestRead) -> str:
    """Короткая опознавалка заявки для списка неоднозначности: комната + суть."""
    room = f"комн. {request.room_number}, " if request.room_number else ""
    return f"{room}«{request.summary}»"


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _join_note(tail: list[str]) -> str | None:
    """Хвост команды после номера — примечание закрытия; пустой — None."""
    joined = " ".join(tail).strip()
    return joined or None
