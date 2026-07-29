"""Consent-gate канала Telegram (spec 0029 §4, issue #127, вариант A юраудита).

Пока гость не нажал кнопку согласия («Продолжить», v2 — тексты и надпись в
`channels/common/consent.py`), канал не зовёт LLM, не создаёт заявок и НЕ
СОХРАНЯЕТ содержимое его сообщений: согласие покрывает «обработку текстов
сообщений» (наш же текст, `docs/legal/consent-text.md`), значит до него хранить
их не на чем. Побочный, но не менее важный эффект — бот-сканер, нашедший бота,
не получает ни одного платного хода.

Тексты, версия и правило «согласие действительно» — общие для каналов
(`channels/common/consent.py`); здесь только телеграмное: inline-кнопка, тост,
снятие клавиатуры и идемпотентность исходящих.

**Идемпотентность без входящего `Message` (P-8).** Обычная опора —
уникальный `idempotency_key` входящего — до согласия не работает (строки нет).
Её заменяют ключи исходящих: экран согласия и приветствие уходят в чат ровно
один раз на пару (диалог, версия согласия). Исключение — `/start`/`/help`: они
показывают экран ВСЕГДА, иначе гость, у которого сообщение с кнопкой удалилось,
остался бы в тупике. Спам командой ограничивает общий rate-limit гостевого чата
(spec 0023) — тот же, что защищает LLM-бюджет.
"""

from __future__ import annotations

import uuid

from hospitality.channels.base import MessageKind, NormalizedMessage
from hospitality.channels.common.consent import (
    CONSENT_VERSION,
    consent_button_label,
    consent_text,
    normalize_language,
)
from hospitality.channels.common.guest_turn import refuse_if_rate_limited
from hospitality.channels.common.store import (
    notification_already_sent,
    record_consent,
    record_outbound_message,
)
from hospitality.channels.telegram.client import TelegramSender
from hospitality.channels.telegram.greeting import greeting_text, is_first_touch_command
from hospitality.channels.telegram.outbound import send_reply
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_guest_consent

logger = get_logger(module=__name__)

# Префикс callback_data кнопки согласия — свой namespace рядом с кнопками
# заявок (`req:<uuid>:<действие>`, keyboards.py). Версия внутри данных: нажатие
# устаревшей кнопки не должно засчитываться как согласие на НОВЫЙ текст.
# "consent:2026-07-29-v3" = 21 байт при лимите Telegram в 64.
_CALLBACK_PREFIX = "consent"
_SEPARATOR = ":"

# Тосты нажавшему (Telegram иначе крутит «часики»). Двуязычные и короткие —
# лимит Telegram 200 символов; развёрнутое приветствие приходит сообщением.
_GRANTED_TOAST = "Спасибо! · Thank you!"
_STALE_TOAST = "Подтвердите согласие ещё раз · Please confirm again"
_ALREADY_TOAST = "Согласие уже получено · Consent already recorded"


async def acknowledge_consent_click(
    normalized: NormalizedMessage, *, sender: TelegramSender
) -> None:
    """Нажатие кнопки согласия гостем, который уже согласился (старое сообщение).

    Гейт такие нажатия не видит: согласие актуально, и апдейт идёт обычным
    путём в `guest.py`. Смысла у повторного нажатия нет — гасим «часики» тостом.
    """
    if parse_consent_callback_data(normalized.text) is None:
        return
    await _answer_callback(normalized, sender=sender, text=_ALREADY_TOAST)


def build_consent_callback_data(consent_version: str = CONSENT_VERSION) -> str:
    return _SEPARATOR.join((_CALLBACK_PREFIX, consent_version))


def parse_consent_callback_data(data: str | None) -> str | None:
    """Версия согласия из callback_data кнопки; None — это не кнопка согласия."""
    if not data:
        return None
    prefix, separator, version = data.partition(_SEPARATOR)
    if prefix != _CALLBACK_PREFIX or not separator:
        return None
    return version


def consent_keyboard(language: str | None) -> dict[str, object]:
    """InlineKeyboardMarkup с единственной кнопкой согласия на языке(ах) гостя."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"✅ {consent_button_label(language)}",
                    "callback_data": build_consent_callback_data(),
                }
            ]
        ]
    }


async def handle_pre_consent(
    conversation_id: uuid.UUID,
    normalized: NormalizedMessage,
    *,
    sender: TelegramSender,
    correlation_id: str,
) -> None:
    """Ветка гостя без актуального согласия (внутри `tenant_context`).

    Единственный способ выйти отсюда — нажать кнопку текущей версии. Всё
    остальное (текст, фото, `/start`, устаревшая кнопка) приводит к показу
    экрана согласия и НИКОГДА не доходит ни до оркестратора, ни до истории
    диалога.
    """
    language = normalize_language(normalized.actor_language)
    if normalized.kind is MessageKind.CALLBACK:
        if parse_consent_callback_data(normalized.text) == CONSENT_VERSION:
            await _grant(conversation_id, normalized, sender=sender, correlation_id=correlation_id)
            return
        # Кнопка чужая или от прежней версии текста: гасим «часики» и показываем
        # актуальный экран — согласие на старый текст новым не считается (§6).
        await _answer_callback(normalized, sender=sender, text=_STALE_TOAST)
    await _prompt(
        conversation_id, normalized, language=language, sender=sender, correlation_id=correlation_id
    )


async def _grant(
    conversation_id: uuid.UUID,
    normalized: NormalizedMessage,
    *,
    sender: TelegramSender,
    correlation_id: str,
) -> None:
    """Записать согласие, снять кнопку и поздороваться (issue #39: одна поверхность)."""
    await record_consent(conversation_id, CONSENT_VERSION)
    logger.info(
        "guest_consent_granted",
        conversation_id=str(conversation_id),
        consent_version=CONSENT_VERSION,
    )
    record_guest_consent("granted")
    await _answer_callback(normalized, sender=sender, text=_GRANTED_TOAST)
    await _remove_keyboard(normalized, sender=sender)

    # Приветствие — один раз на (диалог, версия): повторная доставка нажатия
    # не должна здороваться дважды (P-8, см. докстринг модуля).
    key = f"guest:consent_granted:{conversation_id}:{CONSENT_VERSION}"
    if await notification_already_sent(key):
        return
    language = normalize_language(normalized.actor_language)
    await send_reply(
        conversation_id,
        normalized.chat_id,
        await greeting_text(language),
        sender=sender,
        correlation_id=correlation_id,
        idempotency_key=key,
    )


async def _prompt(
    conversation_id: uuid.UUID,
    normalized: NormalizedMessage,
    *,
    language: str | None,
    sender: TelegramSender,
    correlation_id: str,
) -> None:
    """Показать экран согласия с кнопкой (или промолчать — см. идемпотентность)."""

    async def reply(text: str) -> None:
        await send_reply(
            conversation_id, normalized.chat_id, text, sender=sender, correlation_id=correlation_id
        )

    # Лимиты ступеней spec 0023 применяются и до согласия: гейт — тоже
    # исходящий трафик, а поток `/start` от сканера обязан упереться в потолок.
    if await refuse_if_rate_limited(
        normalized.chat_id, external_id=normalized.chat_id, reply=reply
    ):
        return

    # Команда первого касания показывает экран всегда (гость мог удалить
    # сообщение с кнопкой); обычное сообщение — один раз на версию текста.
    on_demand = normalized.kind is MessageKind.TEXT and is_first_touch_command(normalized.text)
    key = f"guest:consent_prompt:{conversation_id}:{CONSENT_VERSION}"
    already_shown = await notification_already_sent(key)
    if already_shown and not on_demand:
        logger.info("guest_consent_required", conversation_id=str(conversation_id), prompted=False)
        return

    text = consent_text(language)
    try:
        sent_id = await sender.send_message(
            normalized.chat_id, text, reply_markup=consent_keyboard(language)
        )
    except Exception as error:  # best-effort, как send_reply: вебхук не роняем
        logger.warning("telegram_send_failed", chat_id=normalized.chat_id, error=str(error))
        return
    # Ключ забирает ПЕРВЫЙ показ — неважно, по команде он был или автоматический;
    # повторные показы по `/start` пишутся без ключа (иначе упёрлись бы в
    # уникальное ограничение messages), и «автоматический» второй раз не придёт.
    await record_outbound_message(
        conversation_id,
        text,
        correlation_id,
        external_message_id=sent_id,
        idempotency_key=None if already_shown else key,
    )
    logger.info(
        "guest_consent_required",
        conversation_id=str(conversation_id),
        consent_version=CONSENT_VERSION,
        language=language or "all",
        prompted=True,
    )
    record_guest_consent("prompted")


async def _answer_callback(
    normalized: NormalizedMessage, *, sender: TelegramSender, text: str
) -> None:
    if normalized.callback_id is None:  # pragma: no cover — контракт нормализации
        return
    try:
        await sender.answer_callback_query(normalized.callback_id, text)
    except Exception as error:  # best-effort: тост не важнее приёма вебхука
        logger.warning("telegram_toast_failed", error=str(error))


async def _remove_keyboard(normalized: NormalizedMessage, *, sender: TelegramSender) -> None:
    """Убрать кнопку под экраном согласия — нажимать её больше незачем."""
    if normalized.reply_to is None:  # pragma: no cover — у CALLBACK reply_to есть всегда
        return
    try:
        await sender.edit_message_reply_markup(
            normalized.chat_id, normalized.reply_to.external_message_id, None
        )
    except Exception as error:  # best-effort, как и остальные вызовы Bot API
        logger.warning("telegram_keyboard_edit_failed", error=str(error))
