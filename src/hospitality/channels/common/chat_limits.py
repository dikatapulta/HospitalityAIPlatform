"""Лимиты гостевого чата (spec 0023): ступени до оркестратора и учёт цены хода.

Вынесено из `guest_turn.py` с приходом денежной ступени и предела длины
(issue #404, spec 0023 §5). Ступени — от жёсткой к мягкой: деньги за сутки,
сообщения за сутки, всплеск за окно. Все отказы — статические тексты без
вызова LLM: звать модель ради отказа значило бы жечь тот самый бюджет, который
лимит защищает.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import ROUND_UP, Decimal

from hospitality.shared.config import get_settings
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_guest_rate_limited
from hospitality.shared.ratelimit import consume_rate_limit, peek_rate_limit
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)

# Доставка ответа гостю — транспортная забота канала: (text) -> None.
ReplySender = Callable[[str], Awaitable[None]]

# Отказы rate-limit (issue #41, spec 0023): статические и трёхъязычные — как
# UNSUPPORTED_REPLY (kk, ru, en с новой строки; #224), язык гостя без LLM
# неизвестен. Тексты честные: «пара минут» — только у короткого окна, у
# дневного потолка ждать до завтра.
RATE_LIMITED_REPLY = (
    "Қатарынан тым көп хабарлама келді — маған қысқа үзіліс керек. Екі-үш минут "
    "күте тұрыңыз немесе қонақүй қызметкеріне хабарласыңыз.\n"
    "Сообщений слишком много подряд — мне нужна небольшая пауза. Подождите, "
    "пожалуйста, пару минут или обратитесь к сотруднику отеля.\n"
    "Too many messages in a row — I need a short pause. Please wait a couple "
    "of minutes or ask a hotel staff member."
)
DAILY_LIMIT_REPLY = (
    "Бұл чаттағы бүгінгі хабарлама лимиті таусылды. Қонақүй қызметкеріне "
    "хабарласыңыз — сізге көмектеседі.\n"
    "На сегодня лимит сообщений в этом чате исчерпан. Пожалуйста, обратитесь "
    "к сотруднику отеля — вам помогут.\n"
    "This chat has reached its daily message limit. Please contact a hotel "
    "staff member — they will help you."
)

# Предел длины сообщения гостя (sec L7, spec 0023 §5): число стоит в тексте
# отказа, поэтому это константа, а не настройка.
MAX_GUEST_MESSAGE_CHARS = 1000
MESSAGE_TOO_LONG_REPLY = (
    "Хабарлама тым ұзын — мен 1000 таңбаға дейін оқимын. Қысқарақ жазыңыз "
    "немесе қонақүй қызметкеріне хабарласыңыз.\n"
    "Сообщение слишком длинное — я читаю до 1000 знаков. Напишите, пожалуйста, "
    "покороче или обратитесь к сотруднику отеля.\n"
    "Your message is too long — I can read up to 1,000 characters. Please make "
    "it shorter or ask a hotel staff member."
)

# Код каталога ошибок (docs/runbooks/errors.md). Лог-код, не AppError: «клиент» —
# гость, он получает вежливый текст, а не HTTP-ошибку. До spec 0027 назывался
# ERR-TELEGRAM-003 (лимит стал общеканальным вместе с `channels/common`).
ERR_GUEST_RATE_LIMITED = "ERR-CHANNEL-003"

# Окно дневных ступеней: бакеты fixed-window при 86400 выровнены по UTC-суткам
# (epoch кратен суткам) — те же сутки, что у бюджета тенанта в шлюзе (spec 0023).
_DAY_SECONDS = 86_400

_SPEND_SCOPE = "guest_chat_spend"
# Счётчик отказов денежной ступени: peek не считает попыток, а отказ гостю
# обязан уйти один раз в сутки, как у остальных ступеней.
_SPEND_REFUSED_SCOPE = "guest_chat_spend_refused"
_MICRO_USD = Decimal(1_000_000)


def _limit_key(rate_limit_key: str) -> str:
    # tenant_id в ключе обязателен (P-4): у Redis нет RLS, изоляция тенантов
    # держится дисциплиной ключей канона consume_rate_limit.
    return f"{current_tenant_id()}:{rate_limit_key}"


def _spend_ceiling_micro_usd() -> int:
    """Дневной потолок чата в микродолларах; 0 — ступень выключена."""
    settings = get_settings()
    share = Decimal(str(settings.guest_chat_daily_budget_share))
    budget = Decimal(str(settings.llm_tenant_daily_budget_usd))
    return max(int(share * budget * _MICRO_USD), 0)


def _log_refusal(scope: str, external_id: str, count: int, limit: int) -> None:
    logger.warning(
        "guest_rate_limited",
        error_code=ERR_GUEST_RATE_LIMITED,
        scope=scope,
        chat_id=external_id,
        count=count,
        limit=limit,
    )
    record_guest_rate_limited(scope)


async def _spend_refusal(key: str, *, external_id: str, spent: int, ceiling: int) -> str | None:
    """Отказ денежной ступени (spec 0023 §5): текст гостю или None — молча.

    Peek попыток не считает, поэтому «один раз в сутки» держит отдельный
    счётчик отказов с лимитом 0: первый отказ — `count == 1 == limit + 1`.
    """
    _log_refusal(_SPEND_SCOPE, external_id, spent, ceiling)
    refusals = await consume_rate_limit(
        _SPEND_REFUSED_SCOPE, key, limit=0, window_seconds=_DAY_SECONDS
    )
    # Redis отказал на втором запросе — лучше лишний отказ гостю, чем молчание.
    first = refusals.first_rejection or not refusals.available
    return DAILY_LIMIT_REPLY if first else None


async def refuse_if_rate_limited(
    rate_limit_key: str,
    *,
    external_id: str,
    reply: ReplySender,
    pause: ReplySender | None = None,
) -> bool:
    """Ступени rate-limit ДО оркестратора (spec 0023): True — ход не продолжается.

    Публичная, потому что ход гостя — не единственный путь, который надо
    ограничивать: consent-gate (spec 0029 §4) отвечает гостю ДО согласия, то
    есть до этой функции в ходе, и обязан считать те же ступени. Одно место
    правила (P-12): дублировать лимиты в канале нельзя.

    Счётные ступени инкрементятся на каждый текст гостя: сообщения, отклонённые
    всплеск-ступенью, продолжают наполнять дневной счётчик — иначе спамер
    получал бы свежий дневной запас после каждого окна. Отказ отправляется
    один раз на окно (`first_rejection`), дальше — молча: N сообщений спама
    не должны рождать N исходящих. «Молча» — для истории диалога: канал,
    передавший `pause`, на каждом таком сообщении получает текст той же
    ступени и показывает его статусом, а не репликой (web, issue #222);
    Telegram `pause` не передаёт — там тишина остаётся. Redis недоступен →
    fail-open на весь ход.
    """
    settings = get_settings()
    key = _limit_key(rate_limit_key)
    limited = False
    reply_text: str | None = None
    # Отказ первой сработавшей ступени — его получает `pause`, когда реплики нет.
    pause_text: str | None = None
    # Денежная ступень первой (самая жёсткая правда — «сегодня всё»). Она сама
    # ничего не тратит — цену хода прибавляет `record_chat_spend` по исходу.
    # Счётные ступени считаются и после неё: исчерпавший деньги чат копит
    # дневной счёт сообщений, как отклонённый всплеском (абзац выше).
    ceiling = _spend_ceiling_micro_usd()
    if ceiling > 0:
        spent = await peek_rate_limit(_SPEND_SCOPE, key, limit=ceiling, window_seconds=_DAY_SECONDS)
        if not spent.available:
            return False  # fail-open на весь ход: таймаут Redis гость ждёт один раз
        if not spent.allowed:
            limited = True
            pause_text = DAILY_LIMIT_REPLY
            reply_text = await _spend_refusal(
                key, external_id=external_id, spent=spent.count, ceiling=ceiling
            )
    tiers = (
        (
            "guest_chat_daily",
            settings.guest_chat_rate_limit_messages_per_day,
            _DAY_SECONDS,
            DAILY_LIMIT_REPLY,
        ),
        (
            "guest_chat_window",
            settings.guest_chat_rate_limit_messages,
            settings.guest_chat_rate_limit_window_seconds,
            RATE_LIMITED_REPLY,
        ),
    )
    for scope, limit, window_seconds, refusal in tiers:
        if limit <= 0:
            # Ступень отключена конфигурацией — страховочный люк (spec 0023).
            continue
        decision = await consume_rate_limit(scope, key, limit=limit, window_seconds=window_seconds)
        if not decision.available:
            # Fail-open (spec 0023, раздел 2): следующую ступень не проверяем,
            # чтобы гость не ждал таймаут Redis дважды за один ход.
            break
        if decision.allowed:
            continue
        _log_refusal(scope, external_id, decision.count, decision.limit)
        # Ответ выбирает только ПЕРВАЯ сработавшая ступень (порядок — от жёсткой
        # к мягкой): когда дневной потолок уже молчит, window-ступень не должна
        # слать «подождите пару минут» на каждое новое окно — ждать-то до завтра
        # (ревью PR #104). Статус паузы — по тому же правилу.
        if not limited:
            pause_text = refusal
            if decision.first_rejection:
                reply_text = refusal
        limited = True
    if reply_text is not None:
        await reply(reply_text)
    elif pause_text is not None and pause is not None:
        await pause(pause_text)
    return limited


async def refuse_if_too_long(guest_text: str, *, external_id: str, reply: ReplySender) -> bool:
    """Предел длины (sec L7, spec 0023 §5): True — ход не продолжается, LLM не зовётся.

    Стоит ПОСЛЕ `refuse_if_rate_limited`: спам длинными текстами глушат ступени,
    и N сообщений не рождают N отказов.
    """
    if len(guest_text) <= MAX_GUEST_MESSAGE_CHARS:
        return False
    _log_refusal("guest_message_length", external_id, len(guest_text), MAX_GUEST_MESSAGE_CHARS)
    await reply(MESSAGE_TOO_LONG_REPLY)
    return True


async def record_chat_spend(rate_limit_key: str, cost_usd: Decimal) -> None:
    """Прибавить цену хода к дневному счёту чата (spec 0023 §5); fail-open."""
    if cost_usd <= 0 or _spend_ceiling_micro_usd() <= 0:
        return
    amount = int((cost_usd * _MICRO_USD).to_integral_value(rounding=ROUND_UP))
    await consume_rate_limit(
        _SPEND_SCOPE,
        _limit_key(rate_limit_key),
        # Решение счётчика здесь не нужно — отказ принимает peek следующего хода.
        limit=_spend_ceiling_micro_usd(),
        window_seconds=_DAY_SECONDS,
        amount=amount,
    )
