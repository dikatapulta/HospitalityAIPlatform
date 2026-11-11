"""Кнопки-кандидаты при неоднозначном `#N` в команде персонала (spec 0021 П-2 п.5, #83).

Дневной номер сбрасывается каждый день отеля, и незакрытая вчерашняя заявка делит
`#N` с сегодняшней — при 50–150 заявках в сутки ежедневно (аудит 05.08, Е-2). Раньше
бот просил набрать полный id (36 символов); теперь отвечает списком кандидатов и
кнопкой на каждого — тап исполняет ту же команду над выбранной заявкой.

Команда и её примечание едут не в `callback_data` (64 байта), а в записи ответа
в `messages`: ключ `staff:pick:<команда>:<nonce>`, примечание — последней строкой
текста с меткой («Примечание: …», «Причина: …»). Шапка начинается с «#», строки
кандидатов — с «• » и однострочны (пробелы схлопнуты), поэтому строка с меткой
бывает только последней и только примечанием. Запись удалил ретеншн (spec 0032) —
кнопка устарела, тап отвечает тостом и ничего не меняет.

Сам переход — общий `_apply_transition` в `staff.py` (P-5); здесь — тексты, кнопки
и разбор записи.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from hospitality.channels.base import NormalizedMessage
from hospitality.channels.common.store import STAFF_PICK_KEY_PREFIX, load_staff_pick
from hospitality.channels.telegram import keyboards
from hospitality.channels.telegram.client import TelegramSender
from hospitality.modules.requests import api as requests_api
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

PICK_STALE = "Кнопка устарела — повторите команду."

# Команда → вопрос шапки. Ключи — те же глаголы, что `staff._STATUS_BY_VERB`.
_QUESTIONS = {
    "start": "Какую взять в работу?",
    "done": "Какую закрыть?",
    "cancel": "Какую отменить?",
}

# Метка строки примечания: у `/start` примечания нет (spec 0021 П-4).
_NOTE_LABELS = {"done": "Примечание", "cancel": "Причина"}


@dataclass(frozen=True)
class PickReply:
    """Ответ на неоднозначную команду: текст, кнопки и ключ записи в `messages`."""

    text: str
    reply_markup: dict[str, object]
    idempotency_key: str


@dataclass(frozen=True)
class Pick:
    """Команда, которую исполнит нажатая кнопка кандидата."""

    verb: str
    note: str | None


def build(
    number: int,
    verb: str,
    candidates: list[requests_api.ServiceRequestRead],
    note: str | None,
) -> PickReply:
    """Список кандидатов `#N` с кнопками; `note` — уже маскированный хвост команды."""
    lines = [
        f"#{number} — таких открытых заявок несколько. {_QUESTIONS[verb]} "
        "Нажмите кнопку под сообщением."
    ]
    lines += [_one_line(f"• {_title(number, c)} · «{c.summary}»") for c in candidates]
    label = _NOTE_LABELS.get(verb)
    if label is not None and note is not None:
        lines.append(f"{label}: {note}")
    buttons = [(c.id, _button(number, c)) for c in candidates]
    return PickReply(
        "\n".join(lines),
        keyboards.pick_keyboard(buttons),
        f"{STAFF_PICK_KEY_PREFIX}{verb}:{uuid.uuid4().hex}",
    )


async def load(conversation_id: uuid.UUID, normalized: NormalizedMessage) -> Pick | None:
    """Команда ответа, под которым нажата кнопка кандидата; None — кнопка устарела."""
    if normalized.reply_to is None:
        return None
    found = await load_staff_pick(conversation_id, normalized.reply_to.external_message_id)
    if found is None:
        return None
    key, text = found
    verb = key.removeprefix(STAFF_PICK_KEY_PREFIX).split(":", 1)[0]
    if verb not in _QUESTIONS:
        return None
    return Pick(verb, _note(verb, text))


async def clear_buttons(normalized: NormalizedMessage, sender: TelegramSender) -> None:
    """Снять кнопки-кандидаты после исполненного выбора (best-effort).

    Иначе второй тап исполнил бы ту же команду над другим кандидатом. Сбой Bot API
    кнопки оставит — как у кнопок уведомления, это удобство, а не инвариант.
    """
    if normalized.reply_to is None:
        return
    try:
        await sender.edit_message_reply_markup(
            normalized.chat_id, normalized.reply_to.external_message_id, None
        )
    except Exception as error:  # best-effort: кнопки — удобство, не инвариант
        logger.info("staff_keyboard_refresh_failed", error=str(error))


def _title(number: int, request: requests_api.ServiceRequestRead) -> str:
    """«#5 от 10.11 · комн. 505»: номер в один день отеля не повторяется — дата различает."""
    title = f"#{number}"
    if request.service_day is not None:
        title += f" от {request.service_day:%d.%m}"
    if request.room_number:
        title += f" · комн. {request.room_number}"
    return title


def _button(number: int, request: requests_api.ServiceRequestRead) -> str:
    """Подпись кнопки — заголовок кандидата; без дня отеля (заявка до миграции 0010) —
    ещё и начало сути, иначе две кнопки могли бы совпасть."""
    title = _one_line(_title(number, request))
    if request.service_day is not None:
        return title
    summary = _one_line(request.summary)
    return f"{title} · {summary[:20]}…" if len(summary) > 20 else f"{title} · {summary}"


def _note(verb: str, text: str) -> str | None:
    label = _NOTE_LABELS.get(verb)
    last = text.rsplit("\n", 1)[-1]
    if label is None or not last.startswith(f"{label}: "):
        return None
    return last.removeprefix(f"{label}: ") or None


def _one_line(text: str) -> str:
    return " ".join(text.split())
