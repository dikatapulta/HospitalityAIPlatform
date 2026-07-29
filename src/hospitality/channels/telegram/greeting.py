"""Первое касание гостя: `/start`, `/help` и приветствие консьержа (issue #39).

Команды первого касания обрабатывает КАНАЛ, детерминированно и без вызова LLM:
раньше текст `/start` уезжал в оркестратор как обычная реплика — модель что-то
отвечала, но спроектированного первого впечатления не было, а вызов стоил
денег на каждом любопытном, нажавшем Start.

Язык приветствия выбирается тем же правилом, что и текст согласия
(`channels/common/consent.languages_for`, spec 0029 §3): известный язык клиента —
одна версия, неизвестный — все три. Имя отеля — `tenants.name` (единственный
источник, `platform/config.py`).
"""

from __future__ import annotations

import uuid

from hospitality.channels.common.consent import languages_for
from hospitality.platform.config import load_tenant_name
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)

# Команды первого касания. `/start <payload>` принимается, payload НЕ парсится:
# он зарезервирован под deep-link гостевой привязки per-stay (DISCUSSION_LOG,
# `t.me/bot?start=<payload>`) — разбор появится вместе с самой привязкой (P-1).
FIRST_TOUCH_COMMANDS = frozenset({"/start", "/help"})

# Как называется отель в приветствии, если реестр недоступен (деградация ниже).
_HOTEL_FALLBACK = {"kk": "қонақүйдің", "ru": "отеля", "en": "the hotel"}

_GREETINGS = {
    "kk": (
        "Сәлеметсіз бе! Мен {hotel} AI-консьержімін. Өтінімді қабылдаймын "
        "(тазалау, жөндеу, room service), қонақүй туралы сұрақтарға жауап беремін "
        "және кез келген сәтте қызметкерді шақырамын — не қажет екенін жазыңыз."
    ),
    "ru": (
        "Здравствуйте! Я AI-консьерж {hotel}. Приму заявку (уборка, ремонт, "
        "room service), отвечу на вопросы об отеле и в любой момент позову "
        "сотрудника — просто напишите, что нужно."
    ),
    "en": (
        "Hello! I'm the AI concierge at {hotel}. I can take a request "
        "(housekeeping, maintenance, room service), answer questions about the "
        "hotel and call a staff member any time — just tell me what you need."
    ),
}


def is_first_touch_command(text: str | None) -> bool:
    """`/start` или `/help` (с payload или суффиксом бота)? — обработать без LLM."""
    if not text:
        return False
    command = text.strip().split(maxsplit=1)[0]
    # "/start@my_bot" — форма из групповых чатов; гостевой чат приватный, но
    # отбрасывать суффикс дешевле, чем однажды его не узнать.
    return command.split("@", maxsplit=1)[0].lower() in FIRST_TOUCH_COMMANDS


async def greeting_text(language: str | None) -> str:
    """Приветствие консьержа с именем отеля (issue #39), на языке(ах) гостя."""
    hotel = await _hotel_phrase()
    return "\n\n".join(
        _GREETINGS[code].format(hotel=hotel.get(code, _HOTEL_FALLBACK[code]))
        for code in languages_for(language)
    )


async def _hotel_phrase() -> dict[str, str]:
    """Как назвать отель в каждом языке; реестр недоступен — общее слово.

    Приветствие без имени лучше, чем отсутствие приветствия: имя — украшение
    первого впечатления, а не условие работы канала (та же деградация, что у
    телефона ресепшена в веб-чате).
    """
    tenant_id: uuid.UUID = current_tenant_id()
    try:
        async with platform_session_scope() as session:
            name = await load_tenant_name(session, tenant_id)
    except AppError as error:
        logger.warning("greeting_hotel_name_unavailable", error_code=error.code)
        return dict(_HOTEL_FALLBACK)
    return {"kk": f"«{name}» қонақүйінің", "ru": f"отеля «{name}»", "en": name}
