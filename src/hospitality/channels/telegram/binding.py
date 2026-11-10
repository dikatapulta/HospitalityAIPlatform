"""Привязка Telegram-гостя к Stay кодом заселения и auth-only до неё (spec 0074 §2).

Тот же код, что в веб-чате (ADR-008 §3), тем же доменным путём
`guests.start_guest_session`; идентичность — `GuestIdentity(kind=telegram,
external_id=chat_id)`, привязка — обычная `GuestSession` этого Stay. Токен сессии
рождается и не выдаётся: Telegram опознаёт гостя по чату
(`guests.resolve_identity_session`), не по cookie.

Непривязанный гость — строгий auth-only (ADR-008 §4 Q7): ни вызова модели, ни
заявки, ни тревоги персоналу. Его ход — `handle_unbound`: ЧП-маркер → текст ЧП
без эскалации; лимит сообщений чата (spec 0023); попытка «<комната> <код>» →
привязка; иначе подсказка. Статические тексты — на языке клиента Telegram,
неизвестен — три подряд (канон приветствия, `consent.languages_for`).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Final

from hospitality.ai import urgency
from hospitality.channels.base import MessageKind, NormalizedMessage
from hospitality.channels.common.chat_limits import ReplySender, refuse_if_rate_limited
from hospitality.channels.common.consent import CONSENT_VERSION, languages_for
from hospitality.modules.guests import api as guests_api
from hospitality.platform.config import load_tenant_config
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_guest_rate_limited
from hospitality.shared.ratelimit import consume_rate_limit
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)

# Лимит попыток кода исчерпан (лог-код, docs/runbooks/errors.md): чат или комната.
ERR_TELEGRAM_CODE_RATE_LIMITED = "ERR-TELEGRAM-009"

# Ступени лимита попыток (spec 0074 §2.1): по чату — своя дверь алерта ERR-OPS-014;
# по комнате — ТОТ ЖЕ scope и ключ, что у веба (`channels/web/service.start_session`),
# чтобы подбор с двух каналов сразу попыток не удваивал.
CHAT_CODE_SCOPE: Final = "guest_code_verify_telegram"
ROOM_CODE_SCOPE: Final = "guest_code_verify"

# Код — шесть цифр в конце сообщения (`482913`, `482-913`, `482 913`); комната —
# последнее слово с цифрой перед ним. Потолок длины комнаты — колонка `stays.room_number`.
_ATTEMPT = re.compile(r"^(?P<head>.*?)(?P<code>\d{3}[ -]?\d{3})\W*$", re.DOTALL)
_WORD = re.compile(r"\w[\w-]*")
_ROOM_MAX_LENGTH: Final = 20

# Как попытка лежит в `messages`: код в истории не хранится (spec 0074 §2.1).
_STORED_ATTEMPT: Final = "{room} [код заселения]"

# Тексты — показаны основателю до кода (founder-review 2026-11-10-pr912).
_PROMPT: Final[dict[str, str]] = {
    "kk": (
        "Бастау үшін бөлме нөмірі мен ресепшен берген карточкадағы тіркелу кодын "
        "бір хабарламамен жіберіңіз — мысалы: 305 123-456."
    ),
    "ru": (
        "Чтобы начать, отправьте одним сообщением номер комнаты и код заселения "
        "с карточки ресепшена — например: 305 123-456."
    ),
    "en": (
        "To start, send your room number and the check-in code from your reception "
        "card in one message — for example: 305 123-456."
    ),
}
_BOUND: Final[dict[str, str]] = {
    "kk": "Дайын, {room} бөлмесі қосылды. Не көмек керек екенін жазыңыз.",
    "ru": "Готово, номер {room} подключён. Напишите, чем помочь.",
    "en": "Done — room {room} is connected. Tell me what you need.",
}
# Отказ и лимит — дословно фразы веб-чата (`channels/web/service.py`).
_REJECTED: Final[dict[str, str]] = {
    "kk": (
        "Код сәйкес келмеді. Бөлме нөмірі мен тіркелу карточкасындағы кодты "
        "тексеріңіз немесе ресепшенге хабарласыңыз."
    ),
    "ru": (
        "Код не подошёл. Проверьте номер комнаты и код с карточки заселения или "
        "обратитесь на ресепшен."
    ),
    "en": (
        "The code didn't match. Check your room number and the check-in card code, "
        "or contact the reception."
    ),
}
_RATE_LIMITED: Final[dict[str, str]] = {
    "kk": "Тым көп әрекет жасалды. Кейінірек қайталап көріңіз немесе ресепшенге хабарласыңыз.",
    "ru": "Слишком много попыток. Попробуйте позже или обратитесь на ресепшен.",
    "en": "Too many attempts. Please try again later or contact the reception.",
}
_RECEPTION_LABEL: Final[dict[str, str]] = {"kk": "Ресепшн", "ru": "Ресепшен", "en": "Reception"}


@dataclass(frozen=True)
class CodeAttempt:
    """Сообщение гостя, разобранное как «<комната> <код>»."""

    room_number: str
    code: str


def parse_code_attempt(text: str | None) -> CodeAttempt | None:
    """Попытка привязки или None — обычное сообщение (spec 0074 §2.1)."""
    if not text:
        return None
    found = _ATTEMPT.match(text.strip())
    if found is None:
        return None
    rooms = [word for word in _WORD.findall(found["head"]) if any(c.isdigit() for c in word)]
    if not rooms or len(rooms[-1]) > _ROOM_MAX_LENGTH:
        return None
    return CodeAttempt(room_number=rooms[-1], code=found["code"])


def storable(normalized: NormalizedMessage) -> NormalizedMessage:
    """Входящее непривязанного гостя для `messages`: попытка — без кода."""
    if normalized.kind is not MessageKind.TEXT:
        return normalized
    attempt = parse_code_attempt(normalized.text)
    if attempt is None:
        return normalized
    stored = _STORED_ATTEMPT.format(room=attempt.room_number)
    return normalized.model_copy(update={"text": stored, "raw_text": stored})


async def resolve_binding(chat_id: str) -> guests_api.ActiveGuestSession | None:
    """Живая привязка чата к Stay; None — гость не привязан (или выехал, Q8)."""
    return await guests_api.resolve_identity_session(guests_api.GuestIdentityKind.TELEGRAM, chat_id)


async def bind_prompt(language: str | None) -> str:
    """Подсказка «пришлите комнату и код» + телефон ресепшена, если он задан."""
    phone, _dial = await _reception_contacts()
    return _localized(_PROMPT, language, phone=phone)


async def handle_unbound(
    conversation_id: uuid.UUID,
    normalized: NormalizedMessage,
    *,
    language: str | None,
    reply: ReplySender,
) -> None:
    """Текст непривязанного гостя: модель не зовётся, заявок и тревог нет (§2)."""
    chat_id = normalized.chat_id
    text = normalized.text or ""
    emergency = urgency.detect_emergency(text)
    if emergency is not None:
        # Текст ЧП ничего не обещает от имени персонала (spec 0034 §3) — его можно
        # дать и непривязанному; тревогу персоналу шлёт только привязанный (§3).
        logger.warning(
            "emergency_unbound_guest",
            kind=emergency.kind.value,
            conversation_id=str(conversation_id),
            chat_id=chat_id,
        )
        phone, dial = await _reception_contacts()
        await reply(urgency.emergency_reply(emergency.language, phone, reception_room_dial=dial))
        return
    if await refuse_if_rate_limited(chat_id, external_id=chat_id, reply=reply):
        return
    # Только маскированный `text` (контракт `NormalizedMessage.raw_text`): из сырого
    # «карта 4111…» разбор вынул бы комнату из цифр карты — в лог и ключ лимита.
    attempt = parse_code_attempt(text)
    if attempt is None:
        logger.info("guest_telegram_unbound", conversation_id=str(conversation_id))
        await reply(await bind_prompt(language))
        return
    await reply(await _bind(chat_id, attempt, language))


async def _bind(chat_id: str, attempt: CodeAttempt, language: str | None) -> str:
    """Попытка привязки: лимиты до bcrypt, затем доменный путь веба (P-12)."""
    if not await _within_code_limits(chat_id, attempt.room_number):
        return _localized(_RATE_LIMITED, language)
    grant = await guests_api.start_guest_session(
        guests_api.GuestSessionStart(
            room_number=attempt.room_number,
            code=attempt.code,
            identity_kind=guests_api.GuestIdentityKind.TELEGRAM,
            identity_external_id=chat_id,
            # Гейт согласия пройден (service.process_update): версия — текущая.
            consent_version=CONSENT_VERSION,
        )
    )
    if grant is None:
        return _localized(_REJECTED, language)
    logger.info("guest_telegram_bound", stay_id=str(grant.stay_id))
    return _localized(_BOUND, language, room=grant.room_number)


async def _within_code_limits(chat_id: str, room_number: str) -> bool:
    """Обе ступени лимита попыток (тратятся каждой попыткой); False — отказ."""
    settings = get_settings()
    limit = settings.guest_code_verify_rate_limit_attempts
    if limit <= 0:
        return True
    steps = (
        (CHAT_CODE_SCOPE, chat_id),
        (ROOM_CODE_SCOPE, f"{current_tenant_id()}:{room_number}"),
    )
    for scope, key in steps:
        decision = await consume_rate_limit(
            scope,
            key,
            limit=limit,
            window_seconds=settings.guest_code_verify_rate_limit_window_seconds,
        )
        if decision.available and not decision.allowed:
            logger.warning(
                "guest_telegram_code_rate_limited",
                error_code=ERR_TELEGRAM_CODE_RATE_LIMITED,
                scope=scope,
                count=decision.count,
                limit=decision.limit,
                chat_id=chat_id,
            )
            record_guest_rate_limited(scope)
            return False
    return True


def _localized(
    texts: dict[str, str], language: str | None, *, phone: str | None = None, room: str = ""
) -> str:
    """Текст на языке(ах) гостя; телефон ресепшена — одной строкой в конце."""
    codes = languages_for(language)
    blocks = [texts[code].format(room=room) for code in codes]
    if phone:
        label = _RECEPTION_LABEL[codes[0]] if len(codes) == 1 else "Ресепшен / Reception"
        blocks.append(f"📞 {label}: {phone}")
    return "\n\n".join(blocks)


async def _reception_contacts() -> tuple[str | None, str | None]:
    """Телефон ресепшена и что набрать в номере; конфиг недоступен — без них."""
    try:
        async with session_scope() as session:
            config = await load_tenant_config(session, current_tenant_id())
    except AppError as error:
        logger.warning("telegram_reception_contacts_unavailable", error_code=error.code)
        return None, None
    return config.reception_phone, config.reception_room_dial
