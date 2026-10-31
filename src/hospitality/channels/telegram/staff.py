"""Команды и кнопки персонала в staff-чате (Task 0017, ADR-011, spec 0021 П-2/П-4).

Заглушка кабинета персонала (Phase 1) для walking skeleton: сотрудник двигает
заявку по жизненному циклу в чате `TELEGRAM_STAFF_CHAT_ID`. Все пути — кнопки,
команды, реплаи — зовут один и тот же `requests.change_request_status`: карта
переходов `STATUS_TRANSITIONS` не обходится (P-5, DoD #38).

    /start  <#N>          new → in_progress («взять в работу»)
    /done   <#N> [текст]   in_progress → done (+примечание «что не сделано»)
    /cancel <#N> <текст>   * → cancelled с причиной «Другое» (spec 0057 §2);
                           без текста — подсказка: причина обязательна

`/assign` упразднён вместе со статусом assigned (ADR-013): на эту команду бот
отвечает подсказкой «сразу /start» — переучивание, не молчание.

Ноль ручного ввода (issue #38 п.2–3):
- inline-кнопки под уведомлением о заявке (`keyboards.py`) — нажатие исполняет
  переход, бот отвечает тостом и перерисовывает кнопки под новый статус;
- команда ответом (reply) на уведомление — заявка резолвится по
  `external_message_id` уведомления (обратный поиск по ключам `messages`),
  причём ТОЛЬКО среди сообщений своего чата: номера сообщений у чатов служб
  свои и постоянно совпадают (spec 0026, issue #206);
- «⚠️ Готово частично» — бот задаёт вопрос (ForceReply), ответ-реплай персонала
  становится примечанием `resolution_note` и закрывает заявку (spec 0021 П-4);
- «❌ Отменить» — список причин под тем же сообщением, «Другое» — тот же вопрос
  с ForceReply (spec 0057 §2, `staff_cancel.py`).

Подтверждение гостю идёт НЕ отсюда, а подписчиком `request.status_changed`
(`notifications.py`, P-6). RBAC нет (любой в staff-чате двигает заявки) —
приемлемо для одного демо-чата Phase 0 (§17.7); «кто сделал» пишется в логи
(`actor_external_id`).
"""

from __future__ import annotations

import uuid

from hospitality.channels.base import MessageKind, NormalizedMessage
from hospitality.channels.common.store import (
    load_request_id_for_staff_message,
    load_staff_notification_target,
    load_staff_prompt,
    record_outbound_message,
)
from hospitality.channels.telegram import keyboards, staff_cancel
from hospitality.channels.telegram.client import TelegramSender
from hospitality.channels.telegram.outbound import send_reply
from hospitality.modules.requests import api as requests_api
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.pii import mask_payment_card_numbers

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
    "/cancel <#N> <причина> (или кнопка «❌ Отменить» — причины списком). "
    "Номер #N — из уведомления (принимается и полный id); "
    "команду можно отправить ответом на уведомление — тогда номер не нужен."
)

# Вопрос с ForceReply (вид ключа из `store.STAFF_PROMPT_KINDS`) → (переход, вопрос,
# тост ожидания): «что не сделано?» (spec 0021 П-4) и «почему отменяете?» (spec 0057 §2).
_PROMPTS: dict[str, tuple[requests_api.RequestStatus, str, str]] = {
    "note_prompt": (
        requests_api.RequestStatus.DONE,
        "Заявка {label}: что не сделано и почему? Ответьте на это сообщение.",
        "Жду примечание к {label} ответом на вопрос.",
    ),
    "cancel_prompt": (
        requests_api.RequestStatus.CANCELLED,
        staff_cancel.CANCEL_QUESTION,
        staff_cancel.CANCEL_WAITING,
    ),
}

# Ответ на упразднённый /assign (ADR-013): персонал недели пользовался старой
# схемой — молчание выглядело бы поломкой, подсказка переучивает.
_ASSIGN_RETIRED = "Шаг /assign упразднён — сразу берите в работу: /start <#N>."

# Потолок дневного номера `#N`: номер живёт в пределах суток одного отеля, где
# заявок сотни (пилот — 310 номеров), поэтому пять цифр — запас на порядок. Он же
# щит: см. `_is_daily_number` (issue #203, NG-3).
_MAX_DAILY_NUMBER_DIGITS = 5

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
    # Команда разбирается из СЫРОГО текста (spec 0031 §2): маскирование карт
    # раз на ~500 калечит uuid заявки в `/done <id>`, и сотрудник получает
    # «Не разобрал» на верную команду (issue #172). Всё, что дальше хранится
    # или уходит гостю, по-прежнему берётся из маскированного `normalized.text`.
    # Фолбэк стоит ради типа (`raw_text: str | None`) и недостижим: `raw_text`
    # заполняет тот же валидатор, что маскирует `text`, — непустой `text` даёт
    # непустой `raw_text`. При `text=""` валидатор кладёт `raw_text=""` и правый
    # операнд `or` действительно выбирается, но обе ветки дают одну и ту же
    # пустую строку (строка выше отсеивает только `None`). Сработает по иной
    # причине — вернётся #172 («Не разобрал» на верную команду), но не утечка:
    # маскированный текст безопаснее сырого (ревью PR #202).
    command_text = normalized.raw_text or normalized.text
    if command_text.lstrip().startswith("/"):
        reply = await _run_command(conversation_id, command_text, normalized, sender=sender)
        await send_reply(
            conversation_id, normalized.chat_id, reply, sender=sender, correlation_id=correlation_id
        )
        return
    # Обычный текст: единственный смысл — ответ-реплай на наш вопрос «что не
    # сделано?» или «почему отменяете?» (ForceReply, spec 0021 П-4, spec 0057 §2).
    # Всё остальное — молчание (S-2).
    prompt = await _prompt_target(conversation_id, normalized)
    if prompt is None:
        return
    request_id, target = prompt
    cancelled = target is requests_api.RequestStatus.CANCELLED
    _, reply = await _apply_transition(
        request_id,
        target,
        resolution_note=normalized.text,
        cancel_reason=requests_api.CancelReason.OTHER if cancelled else None,
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
    request_id, action, reason = parsed.request_id, parsed.action, parsed.reason
    logger.info(
        "staff_callback_received",
        action=action.value,
        request_id=str(request_id),
        actor=normalized.actor_external_id,
        cancel_reason=reason.value if reason is not None else None,
    )

    if action is keyboards.CallbackAction.DONE_NOTE or reason is requests_api.CancelReason.OTHER:
        kind = "note_prompt" if action is keyboards.CallbackAction.DONE_NOTE else "cancel_prompt"
        await _ask_note(
            conversation_id,
            request_id,
            normalized,
            kind=kind,
            sender=sender,
            correlation_id=correlation_id,
        )
        return
    if action in (keyboards.CallbackAction.CANCEL, keyboards.CallbackAction.BACK):
        try:
            request = await requests_api.get_request(request_id)
        except AppError as error:
            await _toast(sender, normalized, _ERROR_HINTS.get(error.code, error.message))
            return
        toast = await staff_cancel.swap_keyboard(
            request,
            _label(request),
            normalized,
            sender,
            reasons=action is keyboards.CallbackAction.CANCEL,
        )
        await _toast(sender, normalized, toast)
        return

    applied, reply = await _apply_transition(
        request_id,
        keyboards.STATUS_BY_ACTION[action],
        resolution_note=None,
        cancel_reason=reason,
        normalized=normalized,
        sender=sender,
        toast=True,
    )
    if not applied:
        return  # ошибка уже отвечена тостом — не спамим группу (S-2)
    await send_reply(
        conversation_id, normalized.chat_id, reply, sender=sender, correlation_id=correlation_id
    )


async def _ask_note(
    conversation_id: uuid.UUID,
    request_id: uuid.UUID,
    normalized: NormalizedMessage,
    *,
    kind: str,
    sender: TelegramSender,
    correlation_id: str,
) -> None:
    """Вопрос персоналу с ForceReply: «Готово частично» (spec 0021 П-4) или
    «Другое» в списке причин отмены (spec 0057 §2) — `kind` из `_PROMPTS`.

    Ответ-реплай на этот вопрос закрывает заявку с примечанием (`_prompt_target`
    находит её и переход по ключу `staff:<kind>:<id>:…`). Ожидание нигде не
    персистится: не ответили — заявка осталась открытой, кнопка задаёт вопрос снова.
    """
    try:
        request = await requests_api.get_request(request_id)
    except AppError as error:
        await _toast(sender, normalized, _ERROR_HINTS.get(error.code, error.message))
        return
    label = _label(request)
    _, question, waiting = _PROMPTS[kind]
    text = question.format(label=label)
    try:
        sent_id = await sender.send_message(
            normalized.chat_id, text, reply_markup={"force_reply": True}
        )
    except Exception as error:  # best-effort: сообщение с клавиатурой дожим не покрывает (#296)
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
        idempotency_key=f"staff:{kind}:{request_id}:{normalized.callback_id}",
    )
    await _toast(sender, normalized, waiting.format(label=label))


async def _run_command(
    conversation_id: uuid.UUID,
    text: str,
    normalized: NormalizedMessage,
    *,
    sender: TelegramSender,
) -> str:
    """Разобрать и исполнить команду; вернуть текст ответа персоналу.

    `text` — сырой текст сообщения (`raw_text`), а не `normalized.text`: см.
    развилку в `handle_staff_message`. Из него берётся только адрес заявки —
    примечание закрытия проходит канон маскирования в `_join_note`.
    `conversation_id` нужен команде-реплаю: заявку ищут в чате самой команды
    (issue #206).
    """
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

    resolved = await _resolve_target(conversation_id, parts, normalized, verb)
    if isinstance(resolved, str):
        return resolved  # готовый ответ персоналу: не найдено / неоднозначно / кривой ввод
    request_id, note = resolved
    if verb not in _VERBS_WITH_NOTE:
        note = None  # /start примечания не несёт
    cancel_reason = None
    if target is requests_api.RequestStatus.CANCELLED:
        # Причина обязательна (spec 0057 §2): текст команды — причина «Другое»,
        # без текста — подсказка вместо отмены.
        if note is None:
            return await _cancel_hint(request_id)
        cancel_reason = requests_api.CancelReason.OTHER
    _, reply = await _apply_transition(
        request_id,
        target,
        resolution_note=note,
        cancel_reason=cancel_reason,
        normalized=normalized,
        sender=sender,
    )
    return reply


async def _cancel_hint(request_id: uuid.UUID) -> str:
    try:
        request = await requests_api.get_request(request_id)
    except AppError as error:
        return f"Не получилось ({error.code}): {_ERROR_HINTS.get(error.code, error.message)}"
    return staff_cancel.reason_hint(request, _label(request))


async def _resolve_target(
    conversation_id: uuid.UUID, parts: list[str], normalized: NormalizedMessage, verb: str
) -> tuple[uuid.UUID, str | None] | str:
    """Найти заявку команды: явный аргумент (#N/UUID) или реплай на уведомление.

    Возвращает (id, примечание-хвост) либо готовый текст ответа персоналу.
    Приоритет у явного аргумента; при команде-реплае весь хвост после глагола —
    примечание (`/done кофе не принесли` ответом на уведомление, #38 п.3).
    """
    explicit = parts[1] if len(parts) > 1 else None
    if explicit is not None and (_is_daily_number(explicit) or _is_uuid(explicit)):
        resolved = await _resolve_request(explicit, verb)
        if isinstance(resolved, str):
            return resolved
        return resolved, _join_note(parts[2:])

    replied = await _replied_request_id(conversation_id, normalized)
    if replied is not None:
        return replied, _join_note(parts[1:])

    if explicit is None:
        return (
            f"Укажите номер заявки: /{verb} <#N> — "
            "или отправьте команду ответом на уведомление о заявке."
        )
    # Цитата уходит в группу И пишется строкой в `messages` на 90 дней, поэтому
    # цитируется только то, что дословно пережило канон в `normalized.text`:
    # разбор рвёт строку по пробелу раньше канона, и `/done 4111-1111-1111 1111`
    # даёт токен из 12 цифр — канону он не кандидат (нужно 13+), хотя весь ряд
    # тот уже схлопнул. Второго маскирования правило не вводит (P-12) — опирается
    # на уже случившееся. Канон на самой цитате — запас: токен может найтись в
    # тексте подстрокой вне закрытого куска, а до #239 общий текст проносил и
    # `/done 4111111111111111 1` (ревью PR #202, NG-3, spec 0031 §2).
    if explicit not in (normalized.text or ""):
        return (
            "Не разобрал команду — укажите номер #N из уведомления "
            "или отправьте команду ответом на само уведомление."
        )
    return (
        f"Не разобрал «{mask_payment_card_numbers(explicit)}» — укажите номер #N из уведомления "
        "или отправьте команду ответом на само уведомление."
    )


async def _apply_transition(
    request_id: uuid.UUID,
    target: requests_api.RequestStatus,
    *,
    resolution_note: str | None,
    normalized: NormalizedMessage,
    sender: TelegramSender,
    cancel_reason: requests_api.CancelReason | None = None,
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
            request_id, target, resolution_note=resolution_note, cancel_reason=cancel_reason
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
        cancel_reason=cancel_reason.value if cancel_reason is not None else None,
    )
    if toast:
        await _toast(sender, normalized, f"Заявка {_label(updated)} → {updated.status.value}.")
    await _refresh_notification_keyboard(updated, normalized, sender)
    result = f"Заявка {_label(updated)} «{updated.summary}» → {updated.status.value}."
    reason_line = staff_cancel.reason_line(updated)
    if reason_line is not None:
        result += f"\n{reason_line}"
    if updated.resolution_note:
        result += f"\nПримечание: {updated.resolution_note}"
    return True, result


async def _refresh_notification_keyboard(
    request: requests_api.ServiceRequestRead,
    normalized: NormalizedMessage,
    sender: TelegramSender,
) -> None:
    """Перерисовать кнопки под уведомлением о заявке после перехода (best-effort).

    Откуда сообщение: у нажатой кнопки — из `reply_to` (кнопка нажата в этом же
    чате); у текстовой команды — обратным поиском по ключу уведомления, ВМЕСТЕ с
    чатом, в котором оно лежит (spec 0026: с маршрутизацией по службам это не
    обязательно чат команды — `/done 5` может прилететь из чата другой службы).
    Сбой Bot API не мешает переходу: устаревшие кнопки гасит карта переходов + тост.
    """
    if normalized.kind is MessageKind.CALLBACK and normalized.reply_to is not None:
        chat_id, message_id = normalized.chat_id, normalized.reply_to.external_message_id
    else:
        target = await load_staff_notification_target(request.id)
        if target is None:
            return
        chat_id, message_id = target
    markup = keyboards.keyboard_for_status(request.id, request.status)
    try:
        await sender.edit_message_reply_markup(chat_id, message_id, markup)
    except Exception as error:  # best-effort: кнопки — удобство, не инвариант
        logger.info("staff_keyboard_refresh_failed", error=str(error))


async def _prompt_target(
    conversation_id: uuid.UUID, normalized: NormalizedMessage
) -> tuple[uuid.UUID, requests_api.RequestStatus] | None:
    """Заявка и переход для ответа-реплая на вопрос бота (`_PROMPTS`).

    Переход берётся из ключа вопроса, не из его текста: оба вопроса кончаются
    «Ответьте на это сообщение», и ответ на «почему отменяете?» не должен
    закрыть заявку как «выполнено». None — сообщение не ответ на наш вопрос
    (обычная переписка группы, бот молчит). Реплай на само уведомление без
    команды — тоже молчание: намерение неочевидно, есть кнопки и команды.
    """
    if normalized.reply_to is None:
        return None
    replied_text = normalized.reply_to.text or ""
    if "Ответьте на это сообщение" not in replied_text:
        return None
    found = await load_staff_prompt(conversation_id, normalized.reply_to.external_message_id)
    if found is None:
        return None
    kind, request_id = found
    return request_id, _PROMPTS[kind][0]


async def _replied_request_id(
    conversation_id: uuid.UUID, normalized: NormalizedMessage
) -> uuid.UUID | None:
    """Заявка из reply-контекста команды (#38 п.3): уведомление или вопрос бота.

    `conversation_id` — диалог самого реплая (чат, откуда пришла команда):
    искать по одному `external_message_id` нельзя, счётчики сообщений чатов
    служб совпадают (spec 0026, issue #206).
    """
    if normalized.reply_to is None:
        return None
    return await load_request_id_for_staff_message(
        conversation_id, normalized.reply_to.external_message_id
    )


async def _resolve_request(raw: str, verb: str) -> uuid.UUID | str:
    """Разобрать явный аргумент команды — дневной номер `#N` или UUID.

    Возвращает `uuid.UUID` (заявка найдена однозначно) либо готовый текст ответа
    персоналу: заявка не найдена или номер неоднозначен (несколько незакрытых с
    этим `#N` — просим уточнить полным id). Ведущий `#` допускается (`/done #12`).

    Признак «это номер» — тот же `_is_daily_number`, что и у вызывающей стороны:
    второй экземпляр предиката (здесь стоял свой `isdigit()`) разошёлся бы с
    щитом молча, а ровно этим и жила находка ревью PR #202.
    """
    if _is_daily_number(raw):
        return await _resolve_by_daily_number(int(raw.lstrip("#")), verb)
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


def _is_daily_number(value: str) -> bool:
    """Аргумент похож на дневной номер `#N`: десятичные цифры не длиннее потолка.

    Щит стоит на потолке, а не на перечислении того, на что аргумент похожим
    быть не должен. Разбор идёт из сырого текста (#172), и в `int()` отсюда
    уходило всё, набранное цифрами: слитный PAN, его кусок (`/done 41111111
    11111111` — карта с пробелом не по границе четвёрок), телефон. Дальше это
    `WHERE daily_number = $1` по колонке `Integer()`: 7–9 цифр возвращаются
    персоналу ответом «Заявка #… не найдена» и ложатся строкой в `messages` на
    90 дней, а 10+ драйвер отбивает исключением, чей ТЕКСТ несёт набранные
    цифры целиком, — оттуда они идут в JSON-лог `unhandled_error` и в Sentry
    (`include_local_variables=False` в `shared/sentry.py` снимает локалы
    фреймов, но не текст исключения), вебхук отдаёт 500, а повтор от Telegram
    глушит дедупликация — команда исчезает молча. Перечисление форм карты этот
    класс не закрывает: три прохода ревью PR #202 нашли по форме каждый.
    Потолок закрывает целиком — у дневного номера он есть по природе (сутки
    одного отеля), и токен длиннее номером заявки не считается никогда: ни кусок
    карты, ни телефон (issue #203, NG-3).

    `isdecimal()`, а не `isdigit()`: последний истинен и для надстрочных («²»),
    на которых падает уже сам `int()` — второй вход в тот же отказ (#203).

    Проверка канона оставлена запасом: при нынешнем потолке ряд из пяти цифр
    кандидатом в PAN (13–19 цифр) не бывает, но если потолок поднимут, слитный
    PAN останется отвергнутым.

    UUID эта проверка не касается: ~0,2 % из них Луна-валидны, и отказ по ней
    вернул бы регрессию #172.
    """
    digits = value.lstrip("#")
    return (
        digits.isdecimal()
        and len(digits) <= _MAX_DAILY_NUMBER_DIGITS
        and mask_payment_card_numbers(digits) == digits
    )


def _join_note(tail: list[str]) -> str | None:
    """Хвост команды после номера — примечание закрытия; пустой — None.

    Команда разобрана из сырого текста (#172), а примечание уходит в
    `service_requests.resolution_note` — в БД и гостю. Поэтому здесь стоит тот
    же канон маскирования, что и на контракте канала: сырым из команды берётся
    только адрес заявки, хранимый текст маскируется (spec 0031 §2, NG-3).
    """
    joined = " ".join(tail).strip()
    return mask_payment_card_numbers(joined) if joined else None
