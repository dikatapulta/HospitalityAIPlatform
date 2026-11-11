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
from hospitality.channels.web.feedback.states import message_feedback
from hospitality.channels.web.request_hint import record_request_hint
from hospitality.channels.web.schemas import ChatMessage, SendMessageResult
from hospitality.modules.guests import api as guests_api
from hospitality.platform.config import (
    TENANT_NOT_CONFIGURED_ERROR_CODE,
    RoomServiceConfig,
    TenantConfig,
    load_tenant_config,
)
from hospitality.platform.models import Tenant
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, session_scope
from hospitality.shared.errors import VALIDATION_ERROR_CODE, AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_guest_rate_limited, record_guest_web_session
from hospitality.shared.ratelimit import consume_rate_limit, peek_rate_limit
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)

CHANNEL = "web"

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_WEB_UNKNOWN_TENANT = "ERR-WEB-001"
ERR_WEB_UNAUTHENTICATED = "ERR-WEB-002"
ERR_WEB_CODE_REJECTED = "ERR-WEB-003"
ERR_WEB_CODE_RATE_LIMITED = "ERR-WEB-004"
ERR_WEB_BINDLINK_RATE_LIMITED = "ERR-WEB-005"
# Ссылка привязки недействительна (погашена или Stay погас). Spec 0033 §9 звала его
# «ERR-GUESTS-BINDLINK-EXPIRED» — формат каталога (ERR-<ОБЛАСТЬ>-<NNN>,
# shared/errors.py) требует номерного кода; семантика та же.
ERR_GUESTS_BINDLINK_EXPIRED = "ERR-GUESTS-006"

# Статические тексты (трёхъязычные, как канон UNSUPPORTED_REPLY: kk, ru, en с
# новой строки; #224): язык гостя до авторизации неизвестен, LLM ради отказа не
# зовётся (в этом весь смысл Q7).
_UNAUTHENTICATED_TEXT = (
    "Чатты пайдалану үшін нөміріңіздегі QR-кодты сканерлеп, ресепшен берген "
    "карточкадағы тіркелу кодын енгізіңіз.\n"
    "Чтобы пользоваться чатом, отсканируйте QR-код в вашем номере и введите "
    "код заселения с карточки ресепшена.\n"
    "To use the chat, scan the QR code in your room and enter the check-in "
    "code from your reception card."
)
_CODE_REJECTED_TEXT = (
    "Код сәйкес келмеді. Бөлме нөмірі мен тіркелу карточкасындағы кодты "
    "тексеріңіз немесе ресепшенге хабарласыңыз.\n"
    "Код не подошёл. Проверьте номер комнаты и код с карточки заселения или "
    "обратитесь на ресепшен.\n"
    "The code didn't match. Check your room number and the check-in card code, "
    "or contact the reception."
)
_CODE_RATE_LIMITED_TEXT = (
    "Тым көп әрекет жасалды. Кейінірек қайталап көріңіз немесе ресепшенге хабарласыңыз.\n"
    "Слишком много попыток. Попробуйте позже или обратитесь на ресепшен.\n"
    "Too many attempts. Please try again later or contact the reception."
)
_BINDLINK_EXPIRED_TEXT = (
    "Бұл QR-код енді жарамсыз — ресепшенге хабарласыңыз.\n"
    "Этот QR больше не действует — обратитесь, пожалуйста, на ресепшен.\n"
    "This QR code is no longer valid — please contact the reception."
)

# Scope лимита привязки по ссылке: ключ Redis и метка `guest_rate_limited_total`
# (по ней алертер узнаёт отказ у входа в чат — ERR-OPS-014).
_BIND_LINK_SCOPE = "guest_bind_link_consume"

# Хвост истории на первый рендер страницы и потолок пачки poll'а.
PAGE_HISTORY_LIMIT = 50


async def resolve_tenant(tenant_slug: str) -> uuid.UUID:
    """Тенант по slug из комнатного QR; неизвестный slug — 404 `ERR-WEB-001`.

    Slug — не секрет и не идентичность (ADR-008 §6): он лишь выбирает контекст,
    внутри которого дальше работают код/сессия; чужой Stay недостижим на уровне
    СУБД (RLS).
    """
    tenant_id, _name = await resolve_hotel(tenant_slug)
    return tenant_id


async def resolve_hotel(tenant_slug: str) -> tuple[uuid.UUID, str]:
    """Тенант и название отеля по slug — для шапки страниц чата и листка (#746).

    Неизвестный slug — 404 `ERR-WEB-001`, как у `resolve_tenant`.
    """
    async with platform_session_scope() as session:
        row = (
            await session.execute(select(Tenant.id, Tenant.name).where(Tenant.slug == tenant_slug))
        ).first()
    if row is None:
        raise AppError(
            code=ERR_WEB_UNKNOWN_TENANT,
            message="Unknown hotel address",
            status_code=404,
        )
    return row.id, row.name


async def room_service_config() -> RoomServiceConfig | None:
    """Room service отеля, если он включён и в нём есть каталог; иначе None.

    Зовётся внутри `tenant_context`. Тенант без конфига — None: room service у
    него не настроен (кнопки в чате нет, маршруты меню — 404, spec 0038 Р-10).
    """
    try:
        async with session_scope() as session:
            config = await load_tenant_config(session, current_tenant_id())
    except AppError as error:
        if error.code != TENANT_NOT_CONFIGURED_ERROR_CODE:
            raise
        return None
    room_service = config.room_service
    if room_service is None or not room_service.enabled or not room_service.catalogs:
        return None
    return room_service


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


# Зеркало потолка схемы `GuestSessionStart.room_number` (колонка `stays.room_number`
# — String(20)): комната проверяется ДО лимитера и bcrypt, иначе строка в тысячи
# символов становилась ключом Redis, а ошибка схемы — 500 с телом запроса в Sentry
# (аудит 26.10, К-1 / #483).
ROOM_NUMBER_MAX_LENGTH = 20


async def _consume_code_limit(
    scope: str, key: str, *, limit: int, window_seconds: int, **log_fields: str
) -> None:
    """Одна ступень лимита ввода кода: учесть попытку, при превышении — 429.

    Значение ≤0 отключает ступень (страховочный люк, как у всех лимитов).
    Fail-open при недоступном Redis — канон 0023; приемлемо при энтропии
    кода 30^6 (обоснование — spec 0027 §1.2). Ключ в лог не пишется: для
    IP-ступени это адрес гостя (issue #173).
    """
    if limit <= 0:
        return
    decision = await consume_rate_limit(scope, key, limit=limit, window_seconds=window_seconds)
    if decision.available and not decision.allowed:
        logger.warning(
            "guest_web_code_rate_limited",
            error_code=ERR_WEB_CODE_RATE_LIMITED,
            scope=scope,
            count=decision.count,
            limit=decision.limit,
            **log_fields,
        )
        record_guest_rate_limited(scope)
        raise AppError(
            code=ERR_WEB_CODE_RATE_LIMITED, message=_CODE_RATE_LIMITED_TEXT, status_code=429
        )


async def start_session(
    room_number: str, code: str, *, client_ip: str
) -> guests_api.GuestSessionGrant:
    """Привязка тройкой тенант+комната+код (внутри `tenant_context`).

    До обращения к БД — две ступени rate-limit (spec 0027 §3.3), обе тратятся
    каждой попыткой, и успешной тоже (P-1: успех не сбрасывает окно;
    легитимных привязок на комнату — единицы):
    - по IP (`guest_code_verify_ip`, адрес — канон `shared/clientip.py`) —
      первой: аноним со slug отеля не должен гонять bcrypt (~230 мс CPU) на
      каждую выдуманную комнату (аудит 26.10, К-1 / #483);
    - по (tenant, room) — против подбора кода с многих клиентов (ADR-008:
      пер-клиентский лимит такой перебор не остановил бы).
    Отказ привязки — 403 без уточнения причины (перечисление комнат запрещено).
    """
    room_number = room_number.strip()
    if not 1 <= len(room_number) <= ROOM_NUMBER_MAX_LENGTH:
        # Потолок держит и роутер (422 на границе); здесь — пустая после strip
        # строка, которая иначе падала бы ошибкой схемы в домене (500).
        raise AppError(
            code=VALIDATION_ERROR_CODE,
            message=f"Room number must be 1–{ROOM_NUMBER_MAX_LENGTH} characters",
            status_code=422,
        )
    settings = get_settings()
    window_seconds = settings.guest_code_verify_rate_limit_window_seconds
    await _consume_code_limit(
        "guest_code_verify_ip",
        client_ip,
        limit=settings.guest_code_verify_ip_rate_limit_attempts,
        window_seconds=window_seconds,
    )
    await _consume_code_limit(
        "guest_code_verify",
        f"{current_tenant_id()}:{room_number}",
        limit=settings.guest_code_verify_rate_limit_attempts,
        window_seconds=window_seconds,
        room_number=room_number,
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
    token: str, *, client_ip: str, current_session_token: str | None = None
) -> guests_api.GuestSessionGrant:
    """Привязка по ссылке с листка гостю (spec 0033 §6; внутри
    `tenant_context`).

    До обращения к БД — rate-limit по IP (канон 0023): у анонима со ссылкой
    нет ключа тенанта, а токен непереборный (256 бит) — лимит лишь гасит шум.
    Бюджет тратит только отказ (канон входа персонала, #207 → #415): гости
    в Wi-Fi отеля выходят в интернет с одного адреса, и группа, сканирующая
    листки почти разом, иначе упиралась бы в 429 на исправных ссылках, а
    повторный вход тем же листком съедал бы бюджет соседей.
    Ссылка многоразова, сессия рождается ТЕМ ЖЕ путём, что при вводе кода
    (P-12, `start_guest_session_by_bind_link`). Все невалидные исходы
    (погашена перевыпуском, выезд, срок Stay вышел, чужой отель) — один
    ответ 403 ERR-GUESTS-006: «QR больше не действует, обратитесь на ресепшен».

    `current_session_token` — cookie браузера: сессия того же Stay оставляет
    гостя в прежнем диалоге (#223).
    """
    settings = get_settings()
    limit = settings.guest_bind_link_consume_rate_limit_attempts
    window_seconds = settings.guest_bind_link_consume_rate_limit_window_seconds
    if limit > 0:
        decision = await peek_rate_limit(
            _BIND_LINK_SCOPE, client_ip, limit=limit, window_seconds=window_seconds
        )
        if decision.available and not decision.allowed:
            logger.warning(
                "guest_bind_link_rate_limited",
                error_code=ERR_WEB_BINDLINK_RATE_LIMITED,
                count=decision.count,
                limit=decision.limit,
            )
            record_guest_rate_limited(_BIND_LINK_SCOPE)
            raise AppError(
                code=ERR_WEB_BINDLINK_RATE_LIMITED,
                message=_CODE_RATE_LIMITED_TEXT,
                status_code=429,
            )

    grant = await guests_api.start_guest_session_by_bind_link(
        guests_api.GuestSessionBind(
            bind_token=token,
            identity_kind=guests_api.WEB_IDENTITY_KIND,
            # Как при вводе кода: идентичность клиента рождается здесь.
            identity_external_id=str(uuid.uuid4()),
            consent_version=CONSENT_VERSION,
            current_session_token=current_session_token,
        )
    )
    if grant is None:
        record_guest_web_session("bind_rejected")
        if limit > 0:
            await consume_rate_limit(
                _BIND_LINK_SCOPE, client_ip, limit=limit, window_seconds=window_seconds
            )
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
) -> SendMessageResult:
    """Ход гостя: сохранить входящее (P-8) → общий ход → синхронные реплики.

    Реплики и записываются в историю (единая с poll'ом), и возвращаются в
    HTTP-ответе вместе со своими id — web-гостю не нужен push, а опрос, который
    принесёт их снова, страница сверяет по id (#117). Пауза — текст отказа
    лимита, когда ход остановлен лимитом чата, а отказ в этом окне уже уходил
    репликой (spec 0023 §3): в историю он не пишется, страница показывает его
    статусом — иначе гость видел бы тишину (#222). Ключ лимита — stay_id
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
        return SendMessageResult(replies=[])
    inbound_id = await insert_inbound_message(conversation_id, normalized, correlation_id)
    if inbound_id is None:
        # Повтор той же отправки (ретрай страницы) — второго хода нет (P-8);
        # исход первого хода страница дозаберёт poll'ом.
        logger.info("web_duplicate_message", client_message_id=str(client_message_id))
        return SendMessageResult(replies=[], duplicate=True)

    replies: list[str] = []
    reply_ids: list[uuid.UUID] = []

    async def reply(reply_text: str) -> None:
        reply_ids.append(
            await record_outbound_message(
                conversation_id, reply_text, correlation_id, external_message_id=None
            )
        )
        replies.append(reply_text)

    pause_text: str | None = None

    async def pause(text: str) -> None:
        nonlocal pause_text
        pause_text = text

    created_request_id = await run_guest_turn(
        conversation_id,
        # Именно normalized.text, не сырой text из запроса: контракт нормализации
        # маскирует платёжные паттерны (spec 0031) — LLM и эскалация обязаны
        # видеть тот же текст, что записан в messages.
        normalized.text,
        inbound_id,
        external_id=external_id,
        rate_limit_key=str(session.stay_id),
        reply=reply,
        pause=pause,
        provider=provider,
        verified_room_number=session.room_number,
        verified_stay_id=session.stay_id,
    )
    if created_request_id is not None:
        # Первая заявка диалога — гостю, где ждать итог: push у веба нет (#223).
        hint = await record_request_hint(conversation_id, created_request_id, correlation_id)
        if hint is not None:
            reply_ids.append(hint[0])
            replies.append(hint[1])
    return SendMessageResult(
        replies=replies,
        reply_ids=reply_ids,
        last_message_id=reply_ids[-1] if reply_ids else inbound_id,
        pause=pause_text,
    )


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
    # Кнопки оценки под итогом выполненной заявки (spec 0076 §5).
    feedback = await message_feedback((row.request_id for row in rows), stay_id=session.stay_id)
    return [
        ChatMessage(
            id=row.id,
            direction="inbound" if row.direction is MessageDirection.INBOUND else "outbound",
            text=row.text or "",
            created_at=row.created_at,
            feedback=feedback.get(row.request_id) if row.request_id is not None else None,
            client_message_id=(
                row.external_message_id if row.direction is MessageDirection.INBOUND else None
            ),
        )
        for row in rows
    ]
