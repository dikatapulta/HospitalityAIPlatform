"""Подписчики-уведомления Telegram (Task 0017, P-6, ADR-011).

Уведомления — подписчики доменных событий, а не вызовы из `modules/requests`
(P-6). Их регистрирует composition root воркера (`hospitality/worker.py`), как
`include_router` в `app.py`; сам модуль requests о них не знает. Оба выполняются в
`tenant_context` события (его ставит доставщик outbox) и шлют через `TelegramSender`.

- `notify_staff_on_request_created` — на `request.created`: уведомить staff-чат о
  новой заявке (+ подсказать команды закрытия).
- `notify_guest_on_request_closed` — на `request.status_changed` (терминальные
  `done`/`cancelled`): сообщить гостю итог в его чат (адрес — по `request_origins`,
  ADR-011) НА ЯЗЫКЕ ГОСТЯ: канонический русский текст → один вызов перевода с
  единственным целевым языком (`ai.translation.translate_for_guest`, урок #71);
  язык — с заявки (`guest_language`), фолбэк — `default_language` тенанта
  (issue #66), деградация перевода — канонический текст (spec 0021 П-1).

Идемпотентность (P-8, at-least-once ADR-005): исход фиксируется исходящим `Message`
с естественным ключом; повторная доставка события уведомление не дублирует. Сбой
ОТПРАВКИ пробрасывается — воркер ретраит с backoff (ADR-009); ключ гасит дубль на
штатной пере-доставке.
"""

from __future__ import annotations

import uuid

import structlog

from hospitality.ai import translation
from hospitality.ai.gateway.api import LlmProvider
from hospitality.channels.telegram import keyboards
from hospitality.channels.telegram.client import TelegramSender
from hospitality.channels.telegram.store import (
    ensure_conversation,
    load_conversation_external_id,
    load_request_origin_conversation,
    notification_already_sent,
    record_outbound_message,
)
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import load_tenant_config
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.events import subscribe
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)


def register(
    *,
    sender: TelegramSender,
    staff_chat_id: str,
    translate_provider: LlmProvider | None = None,
) -> None:
    """Подписать уведомления Telegram на доменные события (Task 0017, P-6).

    Зовётся composition root воркера (`hospitality/worker.py`); тесты зовут с
    фейк-отправителем. Замыкания связывают отправитель и staff-chat-id с
    обработчиками — сами события их не несут. `translate_provider` переопределяют
    тесты (Fake); прод передаёт None → боевая модель. Один провайдер на оба
    перевода: суть → русский для персонала (баг #71) и статусные сообщения →
    язык гостя (spec 0021 П-1).
    """

    async def on_request_created(event: requests_api.RequestCreated) -> None:
        await notify_staff_on_request_created(
            event,
            sender=sender,
            staff_chat_id=staff_chat_id,
            translate_provider=translate_provider,
        )

    async def on_request_status_changed(event: requests_api.RequestStatusChanged) -> None:
        await notify_guest_on_request_closed(
            event, sender=sender, translate_provider=translate_provider
        )

    subscribe(requests_api.RequestCreated, on_request_created)
    subscribe(requests_api.RequestStatusChanged, on_request_status_changed)


# Канонические русские шаблоны статусных сообщений гостю (spec 0021 П-1).
# Русский — исходный язык платформы; целевой язык сообщения — язык гостя
# (перевод одним вызовом), поэтому двуязычных склеек «RU / EN» здесь больше нет.
GUEST_DONE_TEXT = "Ваша заявка «{summary}» выполнена. Спасибо!"
GUEST_CANCELLED_TEXT = (
    "К сожалению, вашу заявку «{summary}» пришлось отменить. "
    "Если она ещё актуальна — напишите мне, пожалуйста."
)

# Терминальный статус → (шаблон, ключ идемпотентности). Нетерминальные переходы
# гостя не беспокоят: «взяли в работу» — внутренняя кухня службы.
_GUEST_CLOSE_TEXTS: dict[requests_api.RequestStatus, tuple[str, str]] = {
    requests_api.RequestStatus.DONE: (GUEST_DONE_TEXT, "guest:request_done:{request_id}"),
    requests_api.RequestStatus.CANCELLED: (
        GUEST_CANCELLED_TEXT,
        "guest:request_cancelled:{request_id}",
    ),
}

# Платформенный дефолт языка гостя, когда нет ни языка на заявке, ни конфига
# тенанта (P-11: языковые дефолты — свойство тенанта, это лишь последний рубеж).
_PLATFORM_FALLBACK_LANGUAGE = "ru"


async def notify_staff_on_request_created(
    event: requests_api.RequestCreated,
    *,
    sender: TelegramSender,
    staff_chat_id: str,
    translate_provider: LlmProvider | None = None,
) -> None:
    """Уведомить staff-чат о новой заявке (подписчик `request.created`).

    Суть гость пишет на своём языке; персонал читает по-русски. Поэтому суть
    переводим на русский отдельным вызовом (`ai.translation`) и показываем перевод
    как «Суть», а оригинал — строкой «Гость написал» (эталон на случай осечки
    перевода, идея основателя). Категория (из конфига тенанта, уже по-русски) и
    номер комнаты — явными строками: по ним персонал действует даже без сути.
    """
    if not staff_chat_id:
        logger.warning("telegram_staff_chat_not_configured", request_id=str(event.request_id))
        return

    idempotency_key = f"staff:request_created:{event.request_id}"
    if await notification_already_sent(idempotency_key):
        logger.info("staff_notification_skipped_duplicate", request_id=str(event.request_id))
        return

    conversation_id = await ensure_conversation(staff_chat_id)
    # Событие несёт только request_id/category_id/summary — комнату и дневной
    # номер дочитываем из заявки (как `notify_guest_on_request_closed`), иначе
    # служба не знает, куда идти (S-1, #37) и как коротко назвать заявку (S-3,
    # #38). Контракт события не расширяем ради этого (остаётся Уровень B).
    request = await requests_api.get_request(event.request_id)
    summary_ru = await _summary_for_staff(event.summary, translate_provider)
    # Дневной номер `#N` (issue #38, заход 2а): короткая метка для глаз/речи и
    # аргумент команд вместо 36-символьного UUID. Доскелетная заявка без номера
    # (до миграции 0010) — фолбэк на id, чтобы уведомление осталось действенным.
    if request.daily_number is not None:
        header = f"🔔 Новая заявка #{request.daily_number}"
        action_line = (
            f"Ход: /start {request.daily_number} (взять в работу) · "
            f"/done {request.daily_number} · /cancel {request.daily_number}"
        )
    else:
        header = "🔔 Новая заявка от гостя"
        action_line = f"id: {event.request_id}\nХод: /start · /done · /cancel + этот id."
    lines = [
        header,
        f"Категория: {await _category_name(event.category_id)}",
        f"Комната: {request.room_number or '—'}",
        f"Суть: {summary_ru}",
    ]
    # Оригинал — только если он отличается от перевода (гость писал не по-русски):
    # для русскоязычного гостя дублировать строку незачем.
    if event.summary.strip() and event.summary.strip() != summary_ru:
        lines.append(f"Гость написал: {event.summary}")
    lines += ["", action_line]
    text = "\n".join(lines)
    # Отправка может упасть — тогда исключение проброшено, воркер ретраит (ключ
    # гасит дубль). Запись — только после успешной отправки (не «соврать» историей).
    # Кнопки — ноль ручного ввода (spec 0021 П-2): клавиатура статуса `new`.
    sent_id = await sender.send_message(
        staff_chat_id, text, reply_markup=keyboards.keyboard_for_status(request.id, request.status)
    )
    await record_outbound_message(
        conversation_id,
        text,
        _current_correlation_id(),
        external_message_id=sent_id,
        idempotency_key=idempotency_key,
    )
    logger.info("staff_notified", request_id=str(event.request_id))


async def notify_guest_on_request_closed(
    event: requests_api.RequestStatusChanged,
    *,
    sender: TelegramSender,
    translate_provider: LlmProvider | None = None,
) -> None:
    """Сообщить гостю итог заявки — done/cancelled (подписчик `request.status_changed`).

    Сообщение уходит на языке гостя (spec 0021 П-1): канонический русский текст
    и, если целевой язык не русский, один вызов перевода с единственным целевым
    языком (урок #71). Сбой перевода не съедает уведомление — уходит канонический
    текст (внутри — суть словами самого гостя).
    """
    close_text = _GUEST_CLOSE_TEXTS.get(event.new_status)
    if close_text is None:
        return  # нетерминальный переход («взяли в работу») гостя не беспокоит
    template, key_template = close_text

    conversation_id = await load_request_origin_conversation(event.request_id)
    if conversation_id is None:
        # Привязки нет: заявка создана не через Telegram (например, curl-ом на API).
        logger.info("guest_notification_skipped_no_origin", request_id=str(event.request_id))
        return

    idempotency_key = key_template.format(request_id=event.request_id)
    if await notification_already_sent(idempotency_key):
        logger.info("guest_notification_skipped_duplicate", request_id=str(event.request_id))
        return

    chat_id = await load_conversation_external_id(conversation_id)
    if chat_id is None:  # pragma: no cover — привязка ссылается на существующий диалог
        return

    request = await requests_api.get_request(event.request_id)
    canonical = template.format(summary=request.summary)
    if request.resolution_note:
        # Примечание персонала (spec 0021 П-4): «что не сделано/почему» или причина
        # отмены. По-русски — переводится гостю вместе со всем текстом одним вызовом.
        canonical += f"\nОт персонала: {request.resolution_note}"
    text, target_language, translated = await _localize_for_guest(
        canonical, request.guest_language, translate_provider
    )
    sent_id = await sender.send_message(chat_id, text)
    await record_outbound_message(
        conversation_id,
        text,
        _current_correlation_id(),
        external_message_id=sent_id,
        idempotency_key=idempotency_key,
    )
    logger.info(
        "guest_notified_closed",
        request_id=str(event.request_id),
        status=event.new_status.value,
        guest_language=target_language,
        translated=translated,
    )


async def _localize_for_guest(
    canonical: str, guest_language: str | None, translate_provider: LlmProvider | None
) -> tuple[str, str, bool]:
    """Текст гостю на целевом языке; возвращает (текст, язык, переводили ли).

    Целевой язык: язык заявки → `default_language` тенанта (поле оживает,
    issue #66) → платформенный «ru». Русский не переводится (канон уже русский);
    сбой перевода — деградация к каноническому тексту (§7.8: уведомление важнее
    перевода), с warning-логом.
    """
    target = guest_language or await _tenant_default_language()
    if target == _PLATFORM_FALLBACK_LANGUAGE:
        return canonical, target, False
    try:
        translated = await translation.translate_for_guest(
            canonical, language_code=target, provider=translate_provider
        )
    except AppError as error:
        logger.warning("guest_translation_failed", error_code=error.code, guest_language=target)
        return canonical, target, False
    return translated, target, True


async def _tenant_default_language() -> str:
    """`default_language` конфига тенанта; нет конфига — платформенный «ru».

    Уведомление обязано уйти даже у ненастроенного тенанта (онбординг не
    завершён, дрейф конфига) — тот же принцип деградации, что у дневного
    номера (`requests.service._hotel_service_day`).
    """
    try:
        async with session_scope() as session:
            config = await load_tenant_config(session, current_tenant_id())
        return config.default_language
    except AppError as error:
        logger.warning("guest_language_default_unavailable", error_code=error.code)
        return _PLATFORM_FALLBACK_LANGUAGE


async def _summary_for_staff(summary: str, translate_provider: LlmProvider | None) -> str:
    """Суть заявки на русском для персонала; при сбое перевода — оригинал (деградация).

    Сбой провайдера LLM (ERR-AI-*) не должен «съесть» уведомление службе: заявка
    важнее перевода. Тогда персонал видит оригинал + категорию + комнату и всё
    равно может действовать (§7.8, тот же дух, что деградация канала)."""
    try:
        return await translation.translate_for_staff(summary, provider=translate_provider)
    except AppError as error:
        logger.warning("staff_summary_translation_failed", error_code=error.code)
        return summary.strip()


async def _category_name(category_id: uuid.UUID) -> str:
    """Человекочитаемое имя категории заявки для уведомления; id как фолбэк."""
    for category in await requests_api.list_categories():
        if category.id == category_id:
            return category.name
    return str(category_id)


def _current_correlation_id() -> str:
    """correlation_id события (доставщик outbox восстановил его в лог-контекст)."""
    value = structlog.contextvars.get_contextvars().get("correlation_id")
    return value if isinstance(value, str) else ""
