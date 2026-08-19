"""Подписчики-уведомления Telegram (Task 0017, P-6, ADR-011).

Уведомления — подписчики доменных событий, а не вызовы из `modules/requests`
(P-6). Их регистрирует composition root воркера (`hospitality/worker.py`), как
`include_router` в `app.py`; сам модуль requests о них не знает. Оба выполняются в
`tenant_context` события (его ставит доставщик outbox) и шлют через `TelegramSender`.

- `notify_staff_on_request_created` — на `request.created`: уведомить staff-чат о
  новой заявке (+ подсказать команды закрытия).
- `notify_guest_on_request_closed` — на `request.status_changed` (терминальные
  `done`/`cancelled`): сообщить гостю итог в его чат (адрес — по `request_origins`,
  ADR-011). Доставка канал-осознанная (spec 0027 §2): диалог telegram — push
  через sender; иной гостевой канал (web) — только запись исходящего `Message`
  (гость заберёт poll'ом), идемпотентность и ключи общие. НА ЯЗЫКЕ ГОСТЯ:
  канонический русский текст → один вызов перевода с единственным целевым
  языком (`ai.translation.translate_for_guest`, урок #71);
  язык — с заявки (`guest_language`), фолбэк — `default_language` тенанта
  (issue #66), деградация перевода — канонический текст (spec 0021 П-1).
  Частичное выполнение (`done` + примечание персонала) — исключение: текст
  собирает модель сразу на языке гостя (`ai.closing_message`, spec 0030),
  деградация — шаблон «выполнена частично» + «От персонала: …».
- `notify_staff_on_conversation_escalated` — на `conversation.escalated`
  (spec 0022, issue #36): staff-чат узнаёт, что гостю пообещали человека
  (`NEEDS_HUMAN` / деградация §7.8 / ЧП-перехват spec 0034). БЕЗ LLM намеренно: в случае
  `llm_unavailable` причина эскалации и есть недоступность ИИ — путь обязан
  не зависеть от него вовсе; персонал видит оригинал реплики гостя.
- `notify_staff_on_request_cancelled_by_guest` — на `request.status_changed`
  (`cancelled` + `initiator=guest`, spec 0025, issue #40): staff-чат узнаёт,
  что гость отменил свою заявку, кнопки исходного уведомления снимаются.
  Гостю уведомление о ЕГО СОБСТВЕННОЙ отмене при этом не шлётся.

Адресат staff-уведомления (spec 0026, issue #80): чат службы по КАТЕГОРИИ
заявки (`TenantConfig.staff_chats_by_category`), фолбэк — дефолтный чат
инсталляции (`TELEGRAM_STAFF_CHAT_ID`, аргумент `default_staff_chat_id`).
Само правило и его деградация живут в `routing.py` — общем для всех, кто пишет
персоналу (spec 0028: у напоминаний о невзятых заявках адресат тот же).
Эскалация категории не имеет (и не может иметь: половина случаев — недоступный
LLM) и всегда идёт в дефолтный чат — «уровень выше».

Идемпотентность (P-8, at-least-once ADR-005): исход фиксируется исходящим `Message`
с естественным ключом; повторная доставка события уведомление не дублирует. Сбой
ОТПРАВКИ пробрасывается — воркер ретраит с backoff (ADR-009); ключ гасит дубль на
штатной пере-доставке.
"""

from __future__ import annotations

from hospitality.ai import closing_message, translation
from hospitality.ai.escalation import EscalationReason
from hospitality.ai.gateway.api import LlmProvider
from hospitality.channels.common.events import ConversationEscalated
from hospitality.channels.common.store import (
    ensure_conversation,
    load_conversation_address,
    load_request_origin_conversation,
    load_staff_notification_target,
    notification_already_sent,
    record_outbound_message,
)
from hospitality.channels.telegram import keyboards
from hospitality.channels.telegram.client import TelegramSender
from hospitality.channels.telegram.normalize import CHANNEL
from hospitality.channels.telegram.routing import (
    ERR_TELEGRAM_STAFF_CHAT_NOT_CONFIGURED,
    category_name,
    current_correlation_id,
    log_routing,
    resolve_category,
    staff_chat_for_category,
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
    default_staff_chat_id: str,
    translate_provider: LlmProvider | None = None,
) -> None:
    """Подписать уведомления Telegram на доменные события (Task 0017, P-6).

    Зовётся composition root воркера (`hospitality/worker.py`); тесты зовут с
    фейк-отправителем. Замыкания связывают отправитель и ДЕФОЛТНЫЙ staff-чат с
    обработчиками — сами события их не несут; чат службы каждый подписчик
    выбирает по категории заявки (spec 0026). `translate_provider` переопределяют
    тесты (Fake); прод передаёт None → боевая модель. Один провайдер на все
    вызовы модели в уведомлениях: суть → русский для персонала (баг #71),
    статусные сообщения → язык гостя (spec 0021 П-1) и сборка текста о частичном
    выполнении (spec 0030).
    """

    async def on_request_created(event: requests_api.RequestCreated) -> None:
        await notify_staff_on_request_created(
            event,
            sender=sender,
            default_staff_chat_id=default_staff_chat_id,
            translate_provider=translate_provider,
        )

    async def on_request_status_changed(event: requests_api.RequestStatusChanged) -> None:
        # Оба уведомления идемпотентны (свои ключи): сбой второго ретраит
        # доставку события, первый на повторе гасится ключом (P-8).
        await notify_guest_on_request_closed(
            event, sender=sender, translate_provider=translate_provider
        )
        await notify_staff_on_request_cancelled_by_guest(
            event, sender=sender, default_staff_chat_id=default_staff_chat_id
        )

    async def on_conversation_escalated(event: ConversationEscalated) -> None:
        await notify_staff_on_conversation_escalated(
            event, sender=sender, default_staff_chat_id=default_staff_chat_id
        )

    subscribe(requests_api.RequestCreated, on_request_created)
    subscribe(requests_api.RequestStatusChanged, on_request_status_changed)
    subscribe(ConversationEscalated, on_conversation_escalated)


# Канонические русские шаблоны статусных сообщений гостю (spec 0021 П-1).
# Русский — исходный язык платформы; целевой язык сообщения — язык гостя
# (перевод одним вызовом), поэтому двуязычных склеек «RU / EN» здесь больше нет.
GUEST_DONE_TEXT = "Ваша заявка «{summary}» выполнена. Спасибо!"
# Заявка закрыта с примечанием персонала = выполнена частично (spec 0021 П-4:
# «частично» — исход `done`, а не отдельный статус). Штатно такой текст собирает
# модель (spec 0030); этот шаблон — деградация, поэтому он честен сам по себе:
# «выполнена» о частично выполненной заявке гость не читает даже при сбое LLM.
GUEST_DONE_PARTIAL_TEXT = (
    "Ваша заявка «{summary}» выполнена частично. Если нужно что-то ещё — напишите мне, пожалуйста."
)
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
    default_staff_chat_id: str,
    translate_provider: LlmProvider | None = None,
) -> None:
    """Уведомить чат службы о новой заявке (подписчик `request.created`).

    Адресат — чат категории заявки, фолбэк — дефолтный чат (spec 0026).
    Суть гость пишет на своём языке; персонал читает по-русски. Поэтому суть
    переводим на русский отдельным вызовом (`ai.translation`) и показываем перевод
    как «Суть», а оригинал — строкой «Гость написал» (эталон на случай осечки
    перевода, идея основателя). Категория (из конфига тенанта, уже по-русски) и
    номер комнаты — явными строками: по ним персонал действует даже без сути.
    """
    category = await resolve_category(event.category_id)
    staff_chat_id = await staff_chat_for_category(category, default_staff_chat_id)
    if not staff_chat_id:
        logger.warning(
            "telegram_staff_chat_not_configured",
            error_code=ERR_TELEGRAM_STAFF_CHAT_NOT_CONFIGURED,
            request_id=str(event.request_id),
        )
        return

    idempotency_key = f"staff:request_created:{event.request_id}"
    if await notification_already_sent(idempotency_key):
        logger.info("staff_notification_skipped_duplicate", request_id=str(event.request_id))
        return

    conversation_id = await ensure_conversation(CHANNEL, staff_chat_id)
    # Событие несёт только request_id/category_id/summary — комнату и дневной
    # номер дочитываем из заявки (как `notify_guest_on_request_closed`), иначе
    # служба не знает, куда идти (S-1, #37) и как коротко назвать заявку (S-3,
    # #38). Контракт события не расширяем ради этого (остаётся Уровень B).
    request = await requests_api.get_request(event.request_id)
    summary_ru = await _summary_for_staff(event.summary, translate_provider)
    # Дневной номер `#N` (issue #38, заход 2а): короткая метка для глаз/речи и
    # аргумент команд вместо 36-символьного UUID. Доскелетная заявка без номера
    # (до миграции 0010) — фолбэк на id, чтобы уведомление осталось действенным.
    # Срочная заявка (spec 0034 §5) — маркер в первой строке: службе важно
    # различать «течёт с потолка» и «поменяйте полотенце» до чтения сути.
    mark = "🚨 СРОЧНО · " if request.is_urgent else ""
    if request.daily_number is not None:
        header = f"{mark}🔔 Новая заявка #{request.daily_number}"
        action_line = (
            f"Ход: /start {request.daily_number} (взять в работу) · "
            f"/done {request.daily_number} · /cancel {request.daily_number}"
        )
    else:
        header = f"{mark}🔔 Новая заявка от гостя"
        action_line = f"id: {event.request_id}\nХод: /start · /done · /cancel + этот id."
    lines = [
        header,
        f"Категория: {category_name(category, event.category_id)}",
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
        current_correlation_id(),
        external_message_id=sent_id,
        idempotency_key=idempotency_key,
    )
    logger.info("staff_notified", request_id=str(event.request_id))
    log_routing(staff_chat_id, category, default_staff_chat_id)


# Строка причины для персонала — по-русски, без кодов (коды — в логах).
_ESCALATION_REASON_TEXTS: dict[EscalationReason, str] = {
    EscalationReason.UNKNOWN_TOOL: "Бот не смог обработать запрос.",
    EscalationReason.TOOL_EXECUTION_FAILED: "Бот не смог оформить заявку.",
    EscalationReason.LLM_UNAVAILABLE: "ИИ-консьерж недоступен — гость получил дежурный ответ.",
    EscalationReason.EMERGENCY: (
        "Гость написал о чрезвычайной ситуации. Бот к нему НЕ подключался — "
        "распознаны только слова в сообщении. Гостю сказано не ждать ответа в чате "
        "и обратиться на ресепшен."
    ),
}

# Заголовок эскалации. У ЧП (spec 0034) он свой: сообщение о пожаре обязано
# читаться в ленте чата с первого символа, а не после третьей строки.
_ESCALATION_HEADERS: dict[EscalationReason, str] = {
    EscalationReason.EMERGENCY: "🚨 ЧП: сообщение гостя",
}
_DEFAULT_ESCALATION_HEADER = "🆘 Гостю нужен сотрудник"

# Реплика гостя обрезается: длинное сообщение упёрло бы текст в лимит Telegram
# (4096), отправка ретраилась бы впустую до ERR-EVENTS-002 — эскалация пропала бы.
_ESCALATION_QUOTE_MAX_CHARS = 400


async def notify_staff_on_conversation_escalated(
    event: ConversationEscalated,
    *,
    sender: TelegramSender,
    default_staff_chat_id: str,
) -> None:
    """Уведомить staff-чат: гостю пообещали человека (подписчик
    `conversation.escalated`, spec 0022, issue #36).

    Намеренно без LLM (перевода нет): при `llm_unavailable` причина эскалации —
    сама недоступность ИИ, путь обязан работать без него. Персонал видит
    оригинал последней реплики и суть просьбы (`action_summary` — по контракту
    инструмента уже на языке гостя); резерв — встроенный переводчик Telegram.

    Адресат — всегда ДЕФОЛТНЫЙ чат, маршрутизация по службам его не касается
    (spec 0026): категории у эскалации нет и быть не может — при `llm_unavailable`
    именно классификация и не состоялась. Дефолтный чат здесь и есть «уровень
    выше», к которому Phase 1 привяжет SLA-эскалации.
    """
    if not default_staff_chat_id:
        # Ретраить бессмысленно: конфигурация от повтора не появится. Событие
        # помечается доставленным, диагноз — по коду в логах/алертах.
        logger.warning(
            "telegram_staff_chat_not_configured",
            error_code=ERR_TELEGRAM_STAFF_CHAT_NOT_CONFIGURED,
            conversation_id=str(event.conversation_id),
            reason=event.reason.value,
        )
        return

    idempotency_key = f"staff:escalated:{event.inbound_message_id}"
    if await notification_already_sent(idempotency_key):
        logger.info(
            "staff_escalation_skipped_duplicate", conversation_id=str(event.conversation_id)
        )
        return

    conversation_id = await ensure_conversation(CHANNEL, default_staff_chat_id)
    quote = event.guest_message.strip()
    if len(quote) > _ESCALATION_QUOTE_MAX_CHARS:
        quote = quote[: _ESCALATION_QUOTE_MAX_CHARS - 1] + "…"
    lines = [
        _ESCALATION_HEADERS.get(event.reason, _DEFAULT_ESCALATION_HEADER),
        f"Чат: {event.chat_id}",
        f"Комната: {event.room_number or '—'}",
    ]
    if event.action_summary:
        lines.append(f"Просьба: {event.action_summary}")
    # .get с резервом: причина без строки (рассинхрон enum ↔ словарь) не должна
    # ронять подписчика — KeyError ретраился бы до ERR-EVENTS-002 и съел эскалацию.
    # Полноту пары стережёт тест (test_escalation.py), это второй рубеж.
    reason_line = _ESCALATION_REASON_TEXTS.get(event.reason, "Бот не смог обработать запрос.")
    lines += [f"Последняя реплика: «{quote}»", "", reason_line]
    text = "\n".join(lines)
    # Сбой отправки пробрасывается — воркер ретраит (ключ гасит дубль); запись —
    # только после успешной отправки (канон notify_staff_on_request_created).
    sent_id = await sender.send_message(default_staff_chat_id, text)
    await record_outbound_message(
        conversation_id,
        text,
        current_correlation_id(),
        external_message_id=sent_id,
        idempotency_key=idempotency_key,
    )
    logger.info(
        "staff_escalation_notified",
        conversation_id=str(event.conversation_id),
        reason=event.reason.value,
        error_code=event.error_code,
    )


async def notify_staff_on_request_cancelled_by_guest(
    event: requests_api.RequestStatusChanged,
    *,
    sender: TelegramSender,
    default_staff_chat_id: str,
) -> None:
    """Уведомить чат службы: гость отменил свою заявку (подписчик
    `request.status_changed`, spec 0025, issue #40).

    Служба видела «Новая заявка #N» и пошла бы исполнять — факт отмены обязан
    дойти. Отмены персонала не дублируются (он сделал их сам; `initiator` там
    None — прежнее поведение). Намеренно без LLM: категория и комната уже
    по-русски, суть — оригиналом гостя (для сопоставления хватает номера;
    резерв — встроенный переводчик Telegram, как у эскалаций).

    Адресат — чат категории заявки, фолбэк — дефолтный (spec 0026): отмену
    обязана увидеть та же служба, что видела создание. Кнопки при этом
    перерисовываются в чате САМОГО уведомления (он мог быть другим, если
    маппинг успели сменить), а не в текущем адресате.

    Вдогонку best-effort снимаются кнопки исходного уведомления (терминальный
    статус → клавиатуры нет): «Готово» по отменённой заявке — путаница.
    """
    if event.new_status is not requests_api.RequestStatus.CANCELLED:
        return
    if event.initiator is not requests_api.RequestInitiator.GUEST:
        return
    request = await requests_api.get_request(event.request_id)
    category = await resolve_category(request.category_id)
    staff_chat_id = await staff_chat_for_category(category, default_staff_chat_id)
    if not staff_chat_id:
        # Ретраить бессмысленно: конфигурация от повтора не появится (канон
        # notify_staff_on_conversation_escalated).
        logger.warning(
            "telegram_staff_chat_not_configured",
            error_code=ERR_TELEGRAM_STAFF_CHAT_NOT_CONFIGURED,
            request_id=str(event.request_id),
        )
        return

    idempotency_key = f"staff:request_cancelled_by_guest:{event.request_id}"
    if await notification_already_sent(idempotency_key):
        logger.info("staff_cancel_skipped_duplicate", request_id=str(event.request_id))
        return

    conversation_id = await ensure_conversation(CHANNEL, staff_chat_id)
    label = f"#{request.daily_number}" if request.daily_number is not None else str(request.id)
    text = "\n".join(
        [
            f"🚫 Гость отменил заявку {label}",
            f"Категория: {category_name(category, request.category_id)}",
            f"Комната: {request.room_number or '—'}",
            f"Суть: {request.summary}",
        ]
    )
    # Сбой отправки пробрасывается — воркер ретраит (ключ гасит дубль); запись —
    # только после успешной отправки (канон notify_staff_on_request_created).
    sent_id = await sender.send_message(staff_chat_id, text)
    await record_outbound_message(
        conversation_id,
        text,
        current_correlation_id(),
        external_message_id=sent_id,
        idempotency_key=idempotency_key,
    )
    target = await load_staff_notification_target(event.request_id)
    if target is not None:
        notification_chat_id, notification_message_id = target
        markup = keyboards.keyboard_for_status(request.id, request.status)  # терминал → None
        try:
            await sender.edit_message_reply_markup(
                notification_chat_id, notification_message_id, markup
            )
        except Exception as error:  # best-effort: кнопки — удобство, не инвариант
            logger.info("staff_keyboard_refresh_failed", error=str(error))
    logger.info("staff_cancel_notified", request_id=str(event.request_id))
    log_routing(staff_chat_id, category, default_staff_chat_id)


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

    Исключение — частичное выполнение (`done` + примечание персонала): такой текст
    собирает модель сразу на языке гостя (spec 0030), потому что склейка «шаблон +
    приписка» врала гостю («выполнена» и тут же «пылесос сломался»). Сбой модели
    деградирует к честному шаблону «выполнена частично» + «От персонала: …».
    """
    close_text = _GUEST_CLOSE_TEXTS.get(event.new_status)
    if close_text is None:
        return  # нетерминальный переход («взяли в работу») гостя не беспокоит
    if event.initiator is requests_api.RequestInitiator.GUEST:
        # Гость закрыл заявку сам (Phase 0 — только отмена, spec 0025):
        # «к сожалению, вашу заявку пришлось отменить» в ответ на собственную
        # отмену — абсурд; подтверждение он уже получил репликой модели.
        logger.info("guest_notification_skipped_guest_initiated", request_id=str(event.request_id))
        return
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

    address = await load_conversation_address(conversation_id)
    if address is None:  # pragma: no cover — привязка ссылается на существующий диалог
        return
    channel, chat_id = address

    request = await requests_api.get_request(event.request_id)
    # `done` + примечание = выполнено частично (spec 0021 П-4): и шаблон другой,
    # и штатный путь другой — текст собирает модель (spec 0030).
    partial = event.new_status is requests_api.RequestStatus.DONE and bool(request.resolution_note)
    if partial:
        template = GUEST_DONE_PARTIAL_TEXT
    canonical = template.format(summary=request.summary)
    if request.resolution_note:
        # Примечание персонала (spec 0021 П-4): «что не сделано/почему» или причина
        # отмены. По-русски — переводится гостю вместе со всем текстом одним вызовом.
        canonical += f"\nОт персонала: {request.resolution_note}"

    target_language = request.guest_language or await _tenant_default_language()
    composed = (
        await _compose_partial(request, target_language, translate_provider) if partial else None
    )
    if composed is not None:
        text, translated = composed, False
    else:
        text, translated = await _localize_for_guest(canonical, target_language, translate_provider)
    # Канал-осознанная доставка (spec 0027 §2): telegram — push через sender;
    # прочие каналы (web) — только запись исходящего, гость заберёт poll'ом.
    # Идемпотентность одна на оба пути — та же строка Message с тем же ключом.
    sent_id = await sender.send_message(chat_id, text) if channel == CHANNEL else None
    await record_outbound_message(
        conversation_id,
        text,
        current_correlation_id(),
        external_message_id=sent_id,
        idempotency_key=idempotency_key,
    )
    logger.info(
        "guest_notified_closed",
        request_id=str(event.request_id),
        status=event.new_status.value,
        channel=channel,
        guest_language=target_language,
        translated=translated,
        # Частичное выполнение: текст собрала модель (spec 0030) или ушёл шаблон-деградация.
        partial=partial,
        composed=composed is not None,
    )


async def _compose_partial(
    request: requests_api.ServiceRequestRead,
    target_language: str,
    provider: LlmProvider | None,
) -> str | None:
    """Живой текст гостю о частичном выполнении (spec 0030); None — деградировать.

    Пустой ответ модели трактуется как сбой: пустое сообщение гостю хуже
    шаблонного. Обе деградации — с warning-логом, связка с причиной по
    `correlation_id` (как у `guest_translation_failed`).
    """
    if request.resolution_note is None:  # pragma: no cover — вызывается только при partial
        return None
    try:
        text = await closing_message.compose_partial_completion(
            summary=request.summary,
            resolution_note=request.resolution_note,
            language_code=target_language,
            provider=provider,
        )
    except AppError as error:
        logger.warning(
            "guest_partial_message_failed", error_code=error.code, guest_language=target_language
        )
        return None
    if not text:
        logger.warning("guest_partial_message_empty", guest_language=target_language)
        return None
    return text


async def _localize_for_guest(
    canonical: str, target: str, translate_provider: LlmProvider | None
) -> tuple[str, bool]:
    """Текст гостю на целевом языке; возвращает (текст, переводили ли).

    Целевой язык считает вызывающая сторона: язык заявки → `default_language`
    тенанта (поле оживает, issue #66) → платформенный «ru». Русский не
    переводится (канон уже русский); сбой перевода — деградация к каноническому
    тексту (§7.8: уведомление важнее перевода), с warning-логом.
    """
    if target == _PLATFORM_FALLBACK_LANGUAGE:
        return canonical, False
    try:
        translated = await translation.translate_for_guest(
            canonical, language_code=target, provider=translate_provider
        )
    except AppError as error:
        logger.warning("guest_translation_failed", error_code=error.code, guest_language=target)
        return canonical, False
    return translated, True


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
