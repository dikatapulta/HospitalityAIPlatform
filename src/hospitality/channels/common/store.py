"""Персистентность диалога — общая для гостевых каналов (Task 0016, spec 0027 §2).

Единственный путь записать Conversation/Message. Каждая функция — своя транзакция
по канону P-4/P-12: вызывается внутри `tenant_context`, открывает `session_scope()`
(RLS проставляет tenant_id сама). Идемпотентность входящих (P-8) держит уникальное
ограничение `messages(tenant_id, idempotency_key)`, а не проверка-перед-вставкой:
между SELECT и INSERT возможна гонка двух доставок одного апдейта, БД её закрывает.

Канал — явный аргумент там, где он выделяет диалог (`ensure_conversation`);
остальные функции адресуются по `conversation_id`/ключам и от канала не зависят.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError

from hospitality.channels.base import NormalizedMessage
from hospitality.channels.common.models import (
    Conversation,
    Message,
    MessageContentKind,
    MessageDirection,
    RequestOrigin,
)
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


async def ensure_conversation(
    channel: str, external_id: str, *, guest_identity_id: uuid.UUID | None = None
) -> uuid.UUID:
    """id диалога по (канал, чат гостя); создаёт при первом сообщении (идемпотентно).

    Гонка двух первых сообщений одного чата закрывается уникальным ограничением
    `(tenant_id, channel, external_id)`: проигравший INSERT падает, повторный SELECT
    находит созданную строку. `guest_identity_id` (spec 0027 §3.2) заполняется
    только при СОЗДАНИИ (web-диалог рождается привязанным); существующий диалог
    не перепривязывается — слияние идентичностей не забота этой функции.
    """
    async with session_scope() as session:
        existing = await session.scalar(
            select(Conversation.id).where(
                Conversation.channel == channel, Conversation.external_id == external_id
            )
        )
        if existing is not None:
            return existing
        conversation = Conversation(
            channel=channel, external_id=external_id, guest_identity_id=guest_identity_id
        )
        session.add(conversation)
        try:
            await session.flush()
        except IntegrityError:
            # Диалог создала параллельная доставка между SELECT и INSERT — берём её.
            await session.rollback()
            found = await session.scalar(
                select(Conversation.id).where(
                    Conversation.channel == channel, Conversation.external_id == external_id
                )
            )
            if found is None:  # pragma: no cover — IntegrityError без строки невозможен
                raise
            return found
        return conversation.id


async def insert_inbound_message(
    conversation_id: uuid.UUID, message: NormalizedMessage, correlation_id: str
) -> uuid.UUID | None:
    """Сохранить входящее сообщение; None — дубликат доставки (второго Message нет, P-8).

    Дубликат распознаётся по нарушению уникальности `(tenant_id, idempotency_key)`:
    повторный вебхук с тем же update_id не создаёт вторую строку и не влечёт второй
    ответ гостю.
    """
    try:
        async with session_scope() as session:
            row = Message(
                conversation_id=conversation_id,
                direction=MessageDirection.INBOUND,
                content_kind=MessageContentKind(message.kind.value),
                text=message.text,
                external_message_id=message.external_message_id,
                idempotency_key=message.idempotency_key,
                correlation_id=correlation_id,
            )
            session.add(row)
            await session.flush()
            message_id = row.id
    except IntegrityError as error:
        # Имя ограничения по NAMING_CONVENTION (shared/db.py): uq_<table>_<column_0>.
        if "uq_messages_tenant_id" not in str(error):
            raise
        return None
    return message_id


async def record_outbound_message(
    conversation_id: uuid.UUID,
    text: str,
    correlation_id: str,
    *,
    external_message_id: str | None,
    idempotency_key: str | None = None,
) -> uuid.UUID:
    """Сохранить исходящий ответ платформы.

    Реплики гостю идемпотентности не требуют (`idempotency_key=None`, NULL —
    Postgres считает NULL-и различными). Уведомления-подписчики (Task 0017,
    P-8) передают естественный ключ (`staff:request_created:<id>`,
    `guest:request_done:<id>`) — повторная доставка события не создаёт второй
    строки: конфликт по `(tenant_id, idempotency_key)` виден вызывающему.
    """
    async with session_scope() as session:
        row = Message(
            conversation_id=conversation_id,
            direction=MessageDirection.OUTBOUND,
            content_kind=MessageContentKind.TEXT,
            text=text,
            external_message_id=external_message_id,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
        )
        session.add(row)
        await session.flush()
        return row.id


async def load_consent_version(conversation_id: uuid.UUID) -> str | None:
    """Версия согласия, данного в этом диалоге (spec 0029 §1); None — согласия нет.

    Актуальна ли версия, решает `channels/common/consent.is_consent_current` —
    правило одно на оба канала, а хранилище у каждого своё.
    """
    async with session_scope() as session:
        return await session.scalar(
            select(Conversation.consent_version).where(Conversation.id == conversation_id)
        )


async def record_consent(conversation_id: uuid.UUID, consent_version: str) -> None:
    """Записать факт согласия на диалог (spec 0029 §1): момент + версия текста.

    Перезапись, а не история: действует последнее согласие (§1). Повторная
    доставка того же нажатия перезапишет ту же пару — операция идемпотентна
    по смыслу, отдельного ключа P-8 не требует.
    """
    async with session_scope() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is not None:  # pragma: no branch — диалог только что создан
            conversation.consent_at = utc_now()
            conversation.consent_version = consent_version


async def load_pending_action(conversation_id: uuid.UUID) -> dict[str, Any] | None:
    """Прочитать состояние гейта P-9 диалога (Task 0017); None — ожидания нет."""
    async with session_scope() as session:
        return await session.scalar(
            select(Conversation.pending_action).where(Conversation.id == conversation_id)
        )


async def set_pending_action(
    conversation_id: uuid.UUID, pending_action: dict[str, Any] | None
) -> None:
    """Записать/очистить состояние гейта P-9 диалога (Task 0017)."""
    async with session_scope() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is not None:  # pragma: no branch — диалог только что создан
            conversation.pending_action = pending_action


# Сколько прошлых реплик отдаём модели как контекст. Окно намеренно ограничено
# (баг #71, находка на staging): без лимита длинный диалог (1) тянет модель
# имитировать собственные прошлые ошибки — в т.ч. галлюцинацию «заявка принята»
# без вызова инструмента; (2) безгранично растит стоимость хода (input-токены);
# (3) однажды упирается в лимит контекста. ~20 сообщений ≈ 10 ходов — достаточно
# для связного диалога консьержа, состояние подтверждения P-9 живёт отдельно
# (conversations.pending_action), а не в этой истории.
MAX_HISTORY_MESSAGES = 20


async def load_dialog_history(
    conversation_id: uuid.UUID, *, exclude_message_id: uuid.UUID
) -> list[tuple[MessageDirection, str]]:
    """Прежние текстовые реплики диалога для контекста оркестратора (Task 0017).

    Текущее входящее исключается по `exclude_message_id` (оно уже сохранено, но
    оркестратор добавит его сам). Не-текст (`unsupported`, NULL text) пропускается.
    Отдаются последние `MAX_HISTORY_MESSAGES` реплик (свежий хвост берём через
    `DESC + LIMIT`), но в хронологическом порядке — как история диалога.
    """
    async with session_scope() as session:
        rows = await session.execute(
            select(Message.direction, Message.text)
            .where(
                Message.conversation_id == conversation_id,
                Message.id != exclude_message_id,
                Message.content_kind == MessageContentKind.TEXT,
                Message.text.is_not(None),
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(MAX_HISTORY_MESSAGES)
        )
        recent = [(direction, text) for direction, text in rows if text is not None]
        recent.reverse()
        return recent


async def record_request_origin(request_id: uuid.UUID, conversation_id: uuid.UUID) -> None:
    """Привязать заявку к диалогу-источнику (Task 0017, ADR-011); идемпотентно.

    Повторная запись того же `request_id` (пере-обработка апдейта) конфликтует по
    `(tenant_id, request_id)` — берётся уже записанная привязка, второй строки нет.
    """
    try:
        async with session_scope() as session:
            session.add(RequestOrigin(request_id=request_id, conversation_id=conversation_id))
            await session.flush()
    except IntegrityError as error:
        if "uq_request_origins_tenant_id" not in str(error):
            raise
        logger.info("request_origin_already_recorded", request_id=str(request_id))


async def load_request_origin_conversation(request_id: uuid.UUID) -> uuid.UUID | None:
    """id диалога-источника заявки (Task 0017); None — привязки нет (заявка не из чата)."""
    async with session_scope() as session:
        conversation_id: uuid.UUID | None = await session.scalar(
            select(RequestOrigin.conversation_id).where(RequestOrigin.request_id == request_id)
        )
    return conversation_id


async def load_conversation_request_ids(conversation_id: uuid.UUID) -> list[uuid.UUID]:
    """id заявок, созданных из этого диалога (spec 0025) — обратная сторона
    привязки `request_origins`: опора снапшота «активные заявки диалога».
    Открытость и порядок определяет модуль requests (`list_open_requests_by_ids`),
    канал отдаёт только свои привязки (RLS ограничивает тенантом, P-4)."""
    async with session_scope() as session:
        rows = await session.scalars(
            select(RequestOrigin.request_id).where(RequestOrigin.conversation_id == conversation_id)
        )
        return list(rows)


async def load_messages_for_page(
    conversation_id: uuid.UUID,
    *,
    after_message_id: uuid.UUID | None,
    limit: int,
) -> list[Message]:
    """Текстовые сообщения диалога для страницы веб-чата (spec 0027 §3.2, poll).

    `after_message_id=None` — свежий ХВОСТ истории (последние `limit`, в
    хронологическом порядке) для первого рендера; с курсором — всё после него
    (доставка исходящих: подтверждения заявок, записанные подписчиком).
    Курсор сравнивается по (created_at, id) сообщения-курсора — id у UUID не
    монотонны.
    """
    async with session_scope() as session:
        if after_message_id is None:
            rows = list(
                await session.scalars(
                    select(Message)
                    .where(
                        Message.conversation_id == conversation_id,
                        Message.content_kind == MessageContentKind.TEXT,
                        Message.text.is_not(None),
                    )
                    .order_by(Message.created_at.desc(), Message.id.desc())
                    .limit(limit)
                )
            )
            rows.reverse()
            return rows
        cursor = await session.get(Message, after_message_id)
        if cursor is None:
            return []
        rows_after = await session.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.content_kind == MessageContentKind.TEXT,
                Message.text.is_not(None),
                or_(
                    Message.created_at > cursor.created_at,
                    and_(Message.created_at == cursor.created_at, Message.id > cursor.id),
                ),
            )
            .order_by(Message.created_at, Message.id)
            .limit(limit)
        )
        return list(rows_after)


async def load_conversation_address(conversation_id: uuid.UUID) -> tuple[str, str] | None:
    """(channel, external_id) диалога; None — диалога нет.

    Опора канал-осознанных уведомлений (spec 0027 §2): подписчик по каналу
    решает, пушить (telegram) или только записать исходящее (web — гость
    заберёт poll'ом).
    """
    async with session_scope() as session:
        row = (
            await session.execute(
                select(Conversation.channel, Conversation.external_id).where(
                    Conversation.id == conversation_id
                )
            )
        ).first()
    return None if row is None else (row[0], row[1])


async def load_request_id_for_staff_message(
    conversation_id: uuid.UUID, external_message_id: str
) -> uuid.UUID | None:
    """Заявка, к которой относится сообщение бота в staff-чате (spec 0021 П-2/П-4).

    Обратный поиск по `external_message_id` исходящего: уведомление о заявке
    (ключ `staff:request_created:<id>`), напоминание о невзятой заявке
    (`staff:request_unclaimed:<id>`, spec 0028) или вопрос о примечании
    (`staff:note_prompt:<id>:…`) — id заявки парсится из ключа, отдельная
    таблица привязки не нужна. None — сообщение не наше или без ключа.
    Белый список префиксов, а не `staff:%`: в третьем сегменте других ключей
    (`staff:escalated:<message_id>`, spec 0022) — НЕ id заявки, реплай на такое
    сообщение не должен резолвиться в несуществующую заявку.

    `conversation_id` — диалог сообщения-РЕПЛАЯ, и он обязателен: адрес
    `external_message_id` уникален только внутри чата (в Telegram `message_id`
    нумеруется на каждый чат свой), а с маршрутизацией по службам (spec 0026)
    чатов у отеля шесть и счётчики идут параллельно. Без этого условия `/done`
    реплаем в чате уборки закрывал бы заявку инженерии с тем же номером
    сообщения — `done` терминален, «выполнено» уходило не тому гостю, работа
    исчезала из очереди (issue #206). Реплай физически возможен только на
    сообщение своего чата, поэтому условие не отсекает ни одного законного
    случая.
    """
    async with session_scope() as session:
        key = await session.scalar(
            select(Message.idempotency_key)
            .where(
                Message.conversation_id == conversation_id,
                Message.external_message_id == external_message_id,
                Message.direction == MessageDirection.OUTBOUND,
                or_(
                    Message.idempotency_key.like("staff:request_created:%"),
                    Message.idempotency_key.like("staff:request_unclaimed:%"),
                    Message.idempotency_key.like("staff:note_prompt:%"),
                ),
            )
            .order_by(Message.created_at.desc())
            .limit(1)
        )
    if key is None:
        return None
    parts = key.split(":")
    try:
        return uuid.UUID(parts[2])
    except (IndexError, ValueError):
        return None


async def load_staff_notification_target(request_id: uuid.UUID) -> tuple[str, str] | None:
    """(chat_id, external_message_id) уведомления staff-чата о заявке (spec 0021 П-2).

    Нужны ОБА: кнопки перерисовываются в том чате, где лежит сообщение, а с
    маршрутизацией по службам (spec 0026) это не обязательно чат, откуда пришла
    команда. Поэтому чат берётся из диалога самого уведомления, а не из
    настроек и не из входящего. None — уведомление не слалось (или без
    external_message_id: фейк-отправитель в тестах).
    """
    async with session_scope() as session:
        row = (
            await session.execute(
                select(Conversation.external_id, Message.external_message_id)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(Message.idempotency_key == f"staff:request_created:{request_id}")
            )
        ).first()
    if row is None or row[0] is None or row[1] is None:
        return None
    return (row[0], row[1])


async def notification_already_sent(idempotency_key: str) -> bool:
    """Уведомление с этим ключом уже отправлено (P-8, Task 0017)?

    Ключ уникален в паре с tenant_id (ограничение messages); RLS ограничивает
    видимость текущим тенантом. Опора идемпотентности подписчиков при повторной
    доставке события.
    """
    async with session_scope() as session:
        existing = await session.scalar(
            select(Message.id).where(Message.idempotency_key == idempotency_key)
        )
    return existing is not None
