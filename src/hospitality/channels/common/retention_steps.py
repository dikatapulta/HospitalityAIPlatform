"""Шаги ретеншна одного отеля (#1053 п. 5): каждый — в своём `try`.

Зовёт только `retention.py` — прогон по всем отелям; перечень шагов, сроков и
порядка — его docstring. Сбой шага — ERROR `ERR-CHANNEL-004` с именем шага, а
следующие шаги этого отеля идут: иначе ошибка одного раздела, даже выключенного
на копии, оставляла бы гостевые тексты дольше срока у всех остальных. Исключение
одно: данные о проживании удаляются, только если в этом прогоне прошли три шага,
снимающие ссылки на Stay (заявки, заказы, брони), — иначе Stay ждёт следующего
прогона. Отель с упавшим шагом считается упавшим (`tenants_failed`), и пульс
прогона не ставится.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Final, NamedTuple, cast

import structlog
from sqlalchemy import delete, exists, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.sql import Executable

from hospitality.channels.common.models import Conversation, Message, UnansweredQuestion
from hospitality.modules.documents import api as documents_api
from hospitality.modules.feedback import api as feedback_api
from hospitality.modules.guests import api as guests_api
from hospitality.modules.housekeeping import api as housekeeping_api
from hospitality.modules.memos import api as memos_api
from hospitality.modules.mentor import api as mentor_api
from hospitality.modules.requests import api as requests_api
from hospitality.modules.room_service import api as room_service_api
from hospitality.modules.showroom import api as showroom_api
from hospitality.modules.table_booking import api as table_booking_api
from hospitality.platform.files import FilePurpose, delete_expired_files
from hospitality.platform.staff_actions import delete_staff_actions_before
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import tenant_context

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8): сбой шага, сбой на одном
# тенанте и падение прогона целиком — один диагноз (инфраструктура/БД, как у
# ERR-EVENTS-004), поэтому один код, в отличие от пары напоминаний.
ERR_CHANNEL_RETENTION_FAILED = "ERR-CHANNEL-004"

# Файлы, присланные гостем, живут столько же, сколько его тексты (spec 0032).
GUEST_FILE_PURPOSES = (FilePurpose.GUEST_DOCUMENT, FilePurpose.REQUEST_PHOTO)

# Шаги, снимающие ссылки на Stay: без них данные о проживании ждут.
STAY_LINK_STEPS: Final = ("requests", "room_service_orders", "table_bookings")

_NO_MENTOR: Final = mentor_api.MentorRetentionStats(
    messages_deleted=0, summaries_cleared=0, ideas_deleted=0
)
_NO_SHOWROOM: Final = showroom_api.ShowroomRetention(anonymized=0, abandoned_deleted=0)
_NO_STAY_DATA: Final = guests_api.StayDataRetentionStats(
    stays_deleted=0, guests_deleted=0, consent_records_deleted=0, deleted_identity_ids=()
)


class RetentionRunStats(NamedTuple):
    """Счётчики одного прогона — опора лога `guest_text_retention_completed`."""

    tenants: int
    messages_deleted: int
    conversations_deleted: int
    requests_anonymized: int
    staff_actions_deleted: int
    turnover_notes_cleared: int
    guest_files_deleted: int
    unanswered_questions_deleted: int
    act_requests_anonymized: int
    act_requests_abandoned_deleted: int
    mentor_messages_deleted: int
    mentor_summaries_cleared: int
    mentor_ideas_deleted: int
    room_service_orders_detached: int
    room_service_orders_abandoned_deleted: int
    table_bookings_detached: int
    table_bookings_abandoned_deleted: int
    showroom_requests_anonymized: int
    showroom_requests_abandoned_deleted: int
    feedback_comments_cleared: int
    memos_deleted: int
    stays_deleted: int
    guests_deleted: int
    consent_records_deleted: int
    # Тенанты, на которых упал прогон или хотя бы один шаг (ERR-CHANNEL-004, #410).
    tenants_failed: int = 0
    # Упавшие шаги всех тенантов (#1053 п. 5).
    steps_failed: int = 0


class _Steps:
    """Шаги одного тенанта: сбой шага — ERROR ERR-CHANNEL-004 с его именем и «пусто»
    вместо итога, следующие шаги идут (#1053 п. 5)."""

    def __init__(self, tenant_id: uuid.UUID) -> None:
        self.tenant_id = tenant_id
        self.failed: list[str] = []

    async def run[T](self, step: str, call: Callable[[], Awaitable[T]], empty: T) -> T:
        try:
            return await call()
        except Exception:
            logger.error(
                "guest_text_retention_failed",
                error_code=ERR_CHANNEL_RETENTION_FAILED,
                tenant_id=str(self.tenant_id),
                step=step,
                exc_info=True,
            )
            self.failed.append(step)
            return empty


async def _rowcount(statement: Executable) -> int:
    async with session_scope() as session:
        return cast("CursorResult[Any]", await session.execute(statement)).rowcount or 0


async def enforce_tenant(tenant_id: uuid.UUID, cutoff: datetime) -> RetentionRunStats:
    """Ретеншн одного тенанта: сообщения → вопросы без ответа → диалоги →
    тексты заявок → журнал → замечания → файлы → запросы АВР → тексты «Наставника» →
    заказы в номер → брони столика → запросы из 3D-витрины → комментарии к оценкам →
    служебные записки → данные о проживании.

    Порядок важен: диалог, чьё последнее сообщение только что удалено,
    становится пустым и уходит в этом же прогоне (его `updated_at` не моложе
    последнего сообщения — запись сообщений `updated_at` не трогает). Вопросы без
    ответа этому порядку не подчиняются вовсе: предикат удаления диалога от них
    не зависит, а `ON DELETE CASCADE` снял бы их и сам. Свой DELETE им нужен по
    другой причине — возраст: у диалога, где гость пишет каждую неделю, каскада
    не будет никогда, а обещание политики дано на текст, а не на диалог.
    """
    steps = _Steps(tenant_id)
    now = utc_now()
    with (
        structlog.contextvars.bound_contextvars(tenant_id=str(tenant_id)),
        tenant_context(tenant_id),
    ):
        messages_deleted = await steps.run(
            "messages", partial(_rowcount, delete(Message).where(Message.created_at < cutoff)), 0
        )
        # Гостевой текст (spec 0036 §6, PII_REGISTRY) — по СОБСТВЕННОМУ
        # возрасту строки: у живого диалога каскада не будет никогда, а
        # обещание политики дано на текст, а не на диалог.
        unanswered_deleted = await steps.run(
            "unanswered_questions",
            partial(
                _rowcount,
                delete(UnansweredQuestion).where(UnansweredQuestion.created_at < cutoff),
            ),
            0,
        )
        # Живой диалог держат либо оставшиеся сообщения, либо свежий
        # updated_at (pending_action, только что данное согласие).
        conversations_deleted = await steps.run(
            "conversations",
            partial(
                _rowcount,
                delete(Conversation).where(
                    Conversation.updated_at < cutoff,
                    ~exists().where(Message.conversation_id == Conversation.id),
                ),
            ),
            0,
        )
        requests_anonymized = await steps.run(
            "requests",
            partial(requests_api.anonymize_expired_request_texts, created_before=cutoff),
            0,
        )
        staff_actions_deleted = await steps.run(
            "staff_actions", partial(delete_staff_actions_before, cutoff), 0
        )
        turnover_notes_cleared = await steps.run(
            "turnover_notes",
            partial(housekeeping_api.clear_expired_inspection_notes, created_before=cutoff),
            0,
        )
        guest_files_deleted = 0
        for purpose in GUEST_FILE_PURPOSES:
            guest_files_deleted += await steps.run(
                f"guest_files:{purpose.value}",
                partial(delete_expired_files, purpose, created_before=cutoff),
                0,
            )
        act_requests_anonymized = await steps.run(
            "act_requests",
            partial(documents_api.anonymize_expired_act_requests, created_before=cutoff),
            0,
        )
        act_requests_abandoned_deleted = await steps.run(
            "act_requests_abandoned",
            partial(
                documents_api.delete_abandoned_act_requests,
                created_before=now - documents_api.ABANDONED_AFTER,
            ),
            0,
        )
        mentor = await steps.run(
            "mentor",
            partial(mentor_api.delete_expired_mentor_texts, created_before=cutoff),
            _NO_MENTOR,
        )
        orders_detached = await steps.run(
            "room_service_orders",
            partial(room_service_api.detach_expired_orders, created_before=cutoff),
            0,
        )
        orders_abandoned_deleted = await steps.run(
            "room_service_orders_abandoned",
            partial(
                room_service_api.delete_abandoned_orders,
                created_before=now - room_service_api.ABANDONED_AFTER,
            ),
            0,
        )
        bookings_detached = await steps.run(
            "table_bookings",
            partial(table_booking_api.detach_expired_bookings, created_before=cutoff),
            0,
        )
        bookings_abandoned_deleted = await steps.run(
            "table_bookings_abandoned",
            partial(
                table_booking_api.delete_abandoned_bookings,
                created_before=now - table_booking_api.ABANDONED_AFTER,
            ),
            0,
        )
        # Свой срок — 365 дней от `created_at` (spec 0069 §3), не `cutoff` гостя.
        showroom = await steps.run(
            "showroom", partial(showroom_api.anonymize_expired, now), _NO_SHOWROOM
        )
        feedback_comments_cleared = await steps.run(
            "feedback", partial(feedback_api.clear_expired_comments, cutoff), 0
        )
        # Отправленные — свой срок отеля от `sent_at` (spec 0077 §8), черновики — `cutoff`.
        memos_deleted = await steps.run(
            "memos", partial(memos_api.delete_expired_memos, drafts_before=cutoff), 0
        )
        stay_data = _NO_STAY_DATA
        postponed_by = [step for step in steps.failed if step in STAY_LINK_STEPS]
        if postponed_by:
            # Ссылки на Stay в чужих таблицах — без FK; удалённый Stay оставил бы им
            # идентификатор без строки. Шаг уже упал громко — Stay ждёт его починки.
            logger.warning(
                "guest_text_retention_stay_data_postponed",
                tenant_id=str(tenant_id),
                failed_steps=postponed_by,
            )
        else:
            # Срок проживания — тот же `cutoff`, от выезда; у записей о согласии — свой.
            stay_data = await steps.run(
                "stay_data",
                partial(
                    guests_api.delete_expired_stay_data,
                    checked_out_before=cutoff,
                    consents_before=now
                    - timedelta(days=get_settings().consent_record_retention_days),
                ),
                _NO_STAY_DATA,
            )
        if stay_data.deleted_identity_ids:
            # Упади этот шаг, ссылка останется идентификатором без строки — человека
            # он не называет, а диалог уйдёт своим сроком. `updated_at` не трогаем:
            # иначе обнуление продлило бы диалогу жизнь на весь срок.
            await steps.run(
                "conversation_identities",
                partial(
                    _rowcount,
                    update(Conversation)
                    .where(Conversation.guest_identity_id.in_(stay_data.deleted_identity_ids))
                    .values(guest_identity_id=None, updated_at=Conversation.updated_at),
                ),
                0,
            )
    return RetentionRunStats(
        1,
        messages_deleted,
        conversations_deleted,
        requests_anonymized,
        staff_actions_deleted,
        turnover_notes_cleared,
        guest_files_deleted,
        unanswered_deleted,
        act_requests_anonymized,
        act_requests_abandoned_deleted,
        mentor.messages_deleted,
        mentor.summaries_cleared,
        mentor.ideas_deleted,
        orders_detached,
        orders_abandoned_deleted,
        bookings_detached,
        bookings_abandoned_deleted,
        showroom.anonymized,
        showroom.abandoned_deleted,
        feedback_comments_cleared,
        memos_deleted,
        stay_data.stays_deleted,
        stay_data.guests_deleted,
        stay_data.consent_records_deleted,
        steps_failed=len(steps.failed),
    )
