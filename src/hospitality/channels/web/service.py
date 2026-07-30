"""Логика канала web (spec 0027 §3): привязка по коду, auth-only, ход гостя.

Контекст тенанта канал ставит САМ, внутри своих маршрутов, — по образцу
`channels/telegram/service._resolve_tenant` (ADR-008 §6): QR-slug — публичная
строка с бумажки, в общий `TenantResolver`-middleware ни slug, ни гостевая
сессия не входят. Анонимная поверхность — ровно две операции: показать
статический экран и проверить код под rate-limit (§11: явное решение об
анонимном доступе).

Auth-only (Q7, решение 22.07): нет/невалидна/истекла сессия → статический
ответ `ERR-WEB-002` с телефоном ресепшена — БЕЗ единого вызова LLM и без
создания заявок. После выезда — тот же ответ (Q8): валидность сессии
перепроверяет `guests.resolve_session` на каждом действии.

Кнопка входа совмещена с согласием на обработку ПД: версия и тексты — общий
канон каналов (`channels/common/consent.py`, spec 0029); в БД пишется
`guest_sessions.consent_version` на каждую привязку.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from hospitality.ai.gateway.api import LlmProvider
from hospitality.channels.base import MessageKind, NormalizedMessage
from hospitality.channels.common.consent import CONSENT_VERSION
from hospitality.channels.common.guest_turn import run_guest_turn
from hospitality.channels.common.models import MessageDirection
from hospitality.channels.common.store import (
    ensure_conversation,
    insert_inbound_message,
    load_messages_for_page,
    record_outbound_message,
)
from hospitality.channels.web.schemas import ChatMessage
from hospitality.modules.guests import api as guests_api
from hospitality.platform.config import TenantConfig, load_tenant_config
from hospitality.platform.models import Tenant
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_guest_rate_limited, record_guest_web_session
from hospitality.shared.ratelimit import consume_rate_limit
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)

CHANNEL = "web"

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_WEB_UNKNOWN_TENANT = "ERR-WEB-001"
ERR_WEB_UNAUTHENTICATED = "ERR-WEB-002"
ERR_WEB_CODE_REJECTED = "ERR-WEB-003"
ERR_WEB_CODE_RATE_LIMITED = "ERR-WEB-004"
ERR_WEB_BINDLINK_RATE_LIMITED = "ERR-WEB-005"
# Ссылка привязки истекла/потреблена. Spec 0033 §9 звала его
# «ERR-GUESTS-BINDLINK-EXPIRED» — формат каталога (ERR-<ОБЛАСТЬ>-<NNN>,
# shared/errors.py) требует номерного кода; семантика та же.
ERR_GUESTS_BINDLINK_EXPIRED = "ERR-GUESTS-006"

# Статические тексты (двуязычные, как канон UNSUPPORTED_REPLY): язык гостя до
# авторизации неизвестен, LLM ради отказа не зовётся (в этом весь смысл Q7).
_UNAUTHENTICATED_TEXT = (
    "Чтобы пользоваться чатом, отсканируйте QR-код в вашем номере и введите "
    "код заселения с карточки ресепшена. "
    "To use the chat, scan the QR code in your room and enter the check-in "
    "code from your reception card."
)
_CODE_REJECTED_TEXT = (
    "Код не подошёл. Проверьте номер комнаты и код с карточки заселения или "
    "обратитесь на ресепшен. "
    "The code didn't match. Check your room number and the check-in card code, "
    "or contact the reception."
)
_CODE_RATE_LIMITED_TEXT = (
    "Слишком много попыток. Попробуйте позже или обратитесь на ресепшен. "
    "Too many attempts. Please try again later or contact the reception."
)
_BINDLINK_EXPIRED_TEXT = (
    "Ссылка устарела. Попросите на ресепшене показать QR ещё раз — или "
    "отсканируйте QR в номере и введите код с карточки. "
    "The link has expired. Ask the reception to show the QR again — or scan "
    "the QR in your room and enter the code from your card."
)

# Хвост истории на первый рендер страницы и потолок пачки poll'а.
PAGE_HISTORY_LIMIT = 50


async def resolve_tenant(tenant_slug: str) -> uuid.UUID:
    """Тенант по slug из комнатного QR; неизвестный slug — 404 `ERR-WEB-001`.

    Slug — не секрет и не идентичность (ADR-008 §6): он лишь выбирает контекст,
    внутри которого дальше работают код/сессия; чужой Stay недостижим на уровне
    СУБД (RLS).
    """
    async with platform_session_scope() as session:
        tenant_id: uuid.UUID | None = await session.scalar(
            select(Tenant.id).where(Tenant.slug == tenant_slug)
        )
    if tenant_id is None:
        raise AppError(
            code=ERR_WEB_UNKNOWN_TENANT,
            message="Unknown hotel address",
            status_code=404,
        )
    return tenant_id


async def unauthenticated_error() -> AppError:
    """401 строгого auth-only (Q7/Q8): статический текст + телефон ресепшена.

    Зовётся внутри `tenant_context`. Конфиг недоступен — текст без телефона
    (деградация не должна прятать сам отказ).
    """
    phone = None
    try:
        async with session_scope() as session:
            config: TenantConfig = await load_tenant_config(session, current_tenant_id())
        phone = config.reception_phone
    except AppError as error:
        logger.warning("web_reception_phone_unavailable", error_code=error.code)
    message = _UNAUTHENTICATED_TEXT
    if phone:
        message += f" — Ресепшен / Reception: {phone}"
    logger.info("guest_web_unauthenticated", error_code=ERR_WEB_UNAUTHENTICATED)
    return AppError(code=ERR_WEB_UNAUTHENTICATED, message=message, status_code=401)


async def start_session(room_number: str, code: str) -> guests_api.GuestSessionGrant:
    """Привязка тройкой тенант+комната+код (внутри `tenant_context`).

    До обращения к БД — rate-limit по (tenant, room) (spec 0027 §3.3, ADR-008:
    перебор с многих клиентов не обходит пер-клиентский лимит, у анонима нет
    чата-ключа). Счётчик инкрементится и на успехе (P-1: успех не сбрасывает
    окно; легитимных привязок на комнату — единицы). Отказ привязки — 403 без
    уточнения причины (перечисление комнат запрещено).
    """
    settings = get_settings()
    limit = settings.guest_code_verify_rate_limit_attempts
    if limit > 0:
        decision = await consume_rate_limit(
            "guest_code_verify",
            f"{current_tenant_id()}:{room_number}",
            limit=limit,
            window_seconds=settings.guest_code_verify_rate_limit_window_seconds,
        )
        # Fail-open при недоступном Redis — канон 0023; приемлемо при энтропии
        # кода 30^6 (обоснование — spec 0027 §1.2).
        if decision.available and not decision.allowed:
            logger.warning(
                "guest_web_code_rate_limited",
                error_code=ERR_WEB_CODE_RATE_LIMITED,
                room_number=room_number,
                count=decision.count,
                limit=decision.limit,
            )
            record_guest_rate_limited("guest_code_verify")
            raise AppError(
                code=ERR_WEB_CODE_RATE_LIMITED,
                message=_CODE_RATE_LIMITED_TEXT,
                status_code=429,
            )

    grant = await guests_api.start_guest_session(
        guests_api.GuestSessionStart(
            room_number=room_number,
            code=code,
            identity_kind=guests_api.WEB_IDENTITY_KIND,
            # У веба нет внешнего провайдера с готовым id: идентичность клиента
            # рождается здесь (каждая привязка — своя, ADR-008 §3).
            identity_external_id=str(uuid.uuid4()),
            consent_version=CONSENT_VERSION,
        )
    )
    if grant is None:
        record_guest_web_session("rejected")
        raise AppError(code=ERR_WEB_CODE_REJECTED, message=_CODE_REJECTED_TEXT, status_code=403)
    record_guest_web_session("started")
    # Диалог рождается привязанным к идентичности (spec 0027 §3.2).
    await ensure_conversation(
        CHANNEL, str(grant.guest_identity_id), guest_identity_id=grant.guest_identity_id
    )
    return grant


async def start_session_from_bind_link(
    token: str, *, client_ip: str
) -> guests_api.GuestSessionGrant:
    """Привязка по одноразовой QR-ссылке ресепшена (spec 0033 §6; внутри
    `tenant_context`).

    До обращения к Redis — rate-limit по IP (канон 0023): у анонима со ссылкой
    нет ключа тенанта, а токен непереборный (256 бит) — лимит лишь гасит шум.
    Потребление — атомарный GETDEL (`guests_api.consume_bind_link`); дальше
    сессия рождается ТЕМ ЖЕ путём, что при вводе кода (P-12,
    `start_guest_session_for_stay`). Все невалидные исходы (истекла,
    потреблена, Stay погас) — один ответ 403 ERR-GUESTS-006
    с подсказкой про повторный QR и обычный ввод кода.
    """
    settings = get_settings()
    limit = settings.guest_bind_link_consume_rate_limit_attempts
    if limit > 0:
        decision = await consume_rate_limit(
            "guest_bind_link_consume",
            client_ip,
            limit=limit,
            window_seconds=settings.guest_bind_link_consume_rate_limit_window_seconds,
        )
        if decision.available and not decision.allowed:
            logger.warning(
                "guest_bind_link_rate_limited",
                error_code=ERR_WEB_BINDLINK_RATE_LIMITED,
                count=decision.count,
                limit=decision.limit,
            )
            record_guest_rate_limited("guest_bind_link_consume")
            raise AppError(
                code=ERR_WEB_BINDLINK_RATE_LIMITED,
                message=_CODE_RATE_LIMITED_TEXT,
                status_code=429,
            )

    stay_id = await guests_api.consume_bind_link(token)
    grant = None
    if stay_id is not None:
        grant = await guests_api.start_guest_session_for_stay(
            guests_api.GuestSessionBind(
                stay_id=stay_id,
                identity_kind=guests_api.WEB_IDENTITY_KIND,
                # Как при вводе кода: идентичность клиента рождается здесь.
                identity_external_id=str(uuid.uuid4()),
                consent_version=CONSENT_VERSION,
            )
        )
    if grant is None:
        record_guest_web_session("bind_rejected")
        raise AppError(
            code=ERR_GUESTS_BINDLINK_EXPIRED, message=_BINDLINK_EXPIRED_TEXT, status_code=403
        )
    record_guest_web_session("bind_started")
    await ensure_conversation(
        CHANNEL, str(grant.guest_identity_id), guest_identity_id=grant.guest_identity_id
    )
    return grant


async def resolve_session(token: str | None) -> guests_api.ActiveGuestSession | None:
    """Валидность сессии на каждом действии (ADR-008 §3); None → auth-only."""
    if not token:
        return None
    return await guests_api.resolve_session(token)


async def handle_guest_message(
    session: guests_api.ActiveGuestSession,
    text: str,
    client_message_id: uuid.UUID,
    *,
    provider: LlmProvider | None,
    correlation_id: str,
) -> tuple[list[str], bool, uuid.UUID | None]:
    """Ход гостя: сохранить входящее (P-8) → общий ход → синхронные реплики.

    Возвращает (реплики этого хода, duplicate, id последнего записанного
    сообщения хода). Реплики и записываются в историю (единая с poll'ом), и
    возвращаются в HTTP-ответе — web-гостю не нужен push; `last_message_id`
    страница ставит курсором poll'а, иначе следующий опрос принёс бы те же
    сообщения второй раз (баг живой проверки 27.07). Ключ лимита — stay_id
    (spec 0027 §3.2: повторный ввод кода не обнуляет чат-лимиты).
    """
    external_id = str(session.guest_identity_id)
    conversation_id = await ensure_conversation(
        CHANNEL, external_id, guest_identity_id=session.guest_identity_id
    )
    normalized = NormalizedMessage(
        channel=CHANNEL,
        chat_id=external_id,
        external_message_id=str(client_message_id),
        # Namespace ключа — как "telegram:update:<id>" (channels/base.py).
        idempotency_key=f"web:msg:{client_message_id}",
        kind=MessageKind.TEXT,
        text=text,
    )
    if normalized.text is None:  # pragma: no cover — контракт нормализации: TEXT ⇒ text
        return [], False, None
    inbound_id = await insert_inbound_message(conversation_id, normalized, correlation_id)
    if inbound_id is None:
        # Повтор той же отправки (ретрай страницы) — второго хода нет (P-8);
        # курсор не двигаем (None) — клиент дозаберёт исход первого хода poll'ом.
        logger.info("web_duplicate_message", client_message_id=str(client_message_id))
        return [], True, None

    replies: list[str] = []
    last_message_id: uuid.UUID = inbound_id

    async def reply(reply_text: str) -> None:
        nonlocal last_message_id
        last_message_id = await record_outbound_message(
            conversation_id, reply_text, correlation_id, external_message_id=None
        )
        replies.append(reply_text)

    await run_guest_turn(
        conversation_id,
        # Именно normalized.text, не сырой text из запроса: контракт нормализации
        # маскирует платёжные паттерны (spec 0031) — LLM и эскалация обязаны
        # видеть тот же текст, что записан в messages.
        normalized.text,
        inbound_id,
        external_id=external_id,
        rate_limit_key=str(session.stay_id),
        reply=reply,
        provider=provider,
        verified_room_number=session.room_number,
    )
    return replies, False, last_message_id


async def list_messages(
    session: guests_api.ActiveGuestSession, after_message_id: uuid.UUID | None
) -> list[ChatMessage]:
    """История/новые сообщения диалога сессии (poll, spec 0027 §3.2)."""
    conversation_id = await ensure_conversation(
        CHANNEL, str(session.guest_identity_id), guest_identity_id=session.guest_identity_id
    )
    rows = await load_messages_for_page(
        conversation_id, after_message_id=after_message_id, limit=PAGE_HISTORY_LIMIT
    )
    return [
        ChatMessage(
            id=row.id,
            direction="inbound" if row.direction is MessageDirection.INBOUND else "outbound",
            text=row.text or "",
            created_at=row.created_at,
        )
        for row in rows
    ]
