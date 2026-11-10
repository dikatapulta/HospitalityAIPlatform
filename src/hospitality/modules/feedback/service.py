"""Сервис модуля feedback: оценка и комментарий гостя (spec 0076 §4).

Оценить можно заявку `done` своего Stay — Stay вызывающий берёт только из сессии
гостя. Одна оценка на заявку, окончательная (§3): повтор той же — прежняя
(двойное касание, P-8), другая — 409. Палец вниз заводит карточку в службе
эскалаций (`cards.py`), комментарий дописывается в неё.

Двойное касание пальца вниз даёт одну карточку (запись «потом» ревью PR #914):
вставка оценки, карточка и `card_request_id` — одна транзакция, а вставка —
`ON CONFLICT DO NOTHING RETURNING`. Второй вызов встаёт на уникальном индексе,
пока первый не закоммитится, и получает «уже есть» — карточку заводит только
вставивший. Комментарий берёт строку `UPDATE … RETURNING` и держит её до конца
своего шага карточки: дописка и вторая попытка карточки не гоняются с оценкой.

Шаг карточки — best-effort (§4.3): оценка уже записана, сбой — `feedback_card_failed`
(ERROR, ERR-FEEDBACK-005) и `passed_to_staff=False` в ответе.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import datetime
from typing import Any, Final, cast

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.feedback import cards
from hospitality.modules.feedback.models import FeedbackRating, RequestFeedback
from hospitality.modules.feedback.schemas import (
    COMMENT_MAX_CHARS,
    FeedbackCount,
    FeedbackRead,
    FeedbackState,
)
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import TENANT_NOT_CONFIGURED_ERROR_CODE, load_tenant_config
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.pii import mask_payment_card_numbers
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)

# Коды каталога (docs/runbooks/errors.md, R-8; spec 0076 §4.5).
ERR_FEEDBACK_DISABLED: Final = "ERR-FEEDBACK-001"
ERR_FEEDBACK_NOT_RATEABLE: Final = "ERR-FEEDBACK-002"
ERR_FEEDBACK_ALREADY_RATED: Final = "ERR-FEEDBACK-003"
ERR_FEEDBACK_COMMENT_NOT_APPLICABLE: Final = "ERR-FEEDBACK-004"
ERR_FEEDBACK_CARD_FAILED: Final = "ERR-FEEDBACK-005"


async def rate_request(
    request_id: uuid.UUID, *, stay_id: uuid.UUID, rating: FeedbackRating
) -> FeedbackRead:
    """Оценить выполненную заявку своего Stay (§4.2); у `down` — карточка (§4.3)."""
    escalation_key = await _enabled_escalation_key()
    rated = await _rateable_request(request_id, stay_id)
    async with session_scope() as session:
        inserted = await session.scalar(
            insert(RequestFeedback)
            .values(
                id=uuid.uuid4(),
                tenant_id=current_tenant_id(),
                request_id=request_id,
                category_id=rated.category_id,
                rating=rating,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "request_id"])
            .returning(RequestFeedback.id)
        )
        if inserted is None:
            return await _repeat(session, request_id, rating)
        card_id = None
        if rating is FeedbackRating.DOWN:
            card_id = await _card_step(rated, escalation_key, comment=None)
            if card_id is not None:
                await _set_card(session, inserted, card_id)
    logger.info(
        "feedback_rated",
        request_id=str(request_id),
        rating=rating.value,
        category_id=str(rated.category_id),
    )
    return FeedbackRead(
        request_id=request_id, rating=rating, commented=False, passed_to_staff=card_id is not None
    )


async def add_comment(request_id: uuid.UUID, *, stay_id: uuid.UUID, comment: str) -> FeedbackRead:
    """Один комментарий к пальцу вниз (§4.2): в карточку, иначе вторая карточка.

    Карточки при оценке не получилось (`card_request_id` NULL) — комментарий
    заводит её сам: вторая попытка доставки (запись «потом» ревью PR #914).
    """
    text = comment.strip()
    if not text or len(text) > COMMENT_MAX_CHARS:
        raise ValueError(f"comment must be 1..{COMMENT_MAX_CHARS} characters")
    escalation_key = await _enabled_escalation_key()
    rated = await _rateable_request(request_id, stay_id)
    masked = mask_payment_card_numbers(text)  # NG-3, spec 0031: до записи
    async with session_scope() as session:
        row = (
            await session.execute(
                update(RequestFeedback)
                .where(
                    RequestFeedback.request_id == request_id,
                    RequestFeedback.rating == FeedbackRating.DOWN,
                    RequestFeedback.comment.is_(None),
                    # Комментарий, обнулённый ретеншном, второго не допускает.
                    RequestFeedback.comment_at.is_(None),
                )
                .values(comment=masked, comment_at=utc_now())
                .returning(RequestFeedback.id, RequestFeedback.card_request_id)
            )
        ).one_or_none()
        if row is None:
            raise AppError(
                code=ERR_FEEDBACK_COMMENT_NOT_APPLICABLE,
                message="Comment is allowed once and only after a thumbs-down",
                status_code=409,
            )
        feedback_id, card_id = row
        passed, outcome = False, "failed"
        if card_id is None:
            new_card = await _card_step(rated, escalation_key, comment=masked)
            if new_card is not None:
                await _set_card(session, feedback_id, new_card)
                passed, outcome = True, "new_card"
        else:
            appended = await _append_step(card_id, rated, escalation_key, masked)
            if appended is not None:
                passed, outcome = True, "appended" if appended else "new_card"
    logger.info("feedback_commented", request_id=str(request_id), outcome=outcome)
    return FeedbackRead(
        request_id=request_id, rating=FeedbackRating.DOWN, commented=True, passed_to_staff=passed
    )


async def feedback_states(
    request_ids: Collection[uuid.UUID], *, stay_id: uuid.UUID
) -> dict[uuid.UUID, FeedbackState]:
    """Состояние виджета (§5): только заявки, которые этот Stay может оценить.

    Оценки выключены — пусто: виджета нет (§4.4).
    """
    if not request_ids or await _escalation_key_if_enabled() is None:
        return {}
    rateable = [
        request.id
        for request in await requests_api.list_requests_by_ids(list(request_ids))
        if request.status is requests_api.RequestStatus.DONE and request.stay_id == stay_id
    ]
    if not rateable:
        return {}
    async with session_scope() as session:
        rows = await session.execute(
            select(
                RequestFeedback.request_id,
                RequestFeedback.rating,
                RequestFeedback.comment_at,
                RequestFeedback.card_request_id,
            ).where(RequestFeedback.request_id.in_(rateable))
        )
        stored = {row.request_id: row for row in rows}
    states: dict[uuid.UUID, FeedbackState] = {}
    for request_id in rateable:
        row = stored.get(request_id)
        states[request_id] = FeedbackState(
            rating=row.rating if row is not None else None,
            commented=row is not None and row.comment_at is not None,
            passed_to_staff=row is not None and row.card_request_id is not None,
        )
    return states


async def count_feedback(
    *, start: datetime, end: datetime, category_ids: Collection[uuid.UUID] | None = None
) -> list[FeedbackCount]:
    """`up` и `down` по службам за `[start, end)` по времени оценки (§6)."""
    query = (
        select(
            RequestFeedback.category_id,
            func.count().filter(RequestFeedback.rating == FeedbackRating.UP),
            func.count().filter(RequestFeedback.rating == FeedbackRating.DOWN),
        )
        .where(RequestFeedback.created_at >= start, RequestFeedback.created_at < end)
        .group_by(RequestFeedback.category_id)
        .order_by(RequestFeedback.category_id)
    )
    if category_ids is not None:
        query = query.where(RequestFeedback.category_id.in_(list(category_ids)))
    async with session_scope() as session:
        rows = await session.execute(query)
        return [FeedbackCount(category_id=row[0], up=row[1], down=row[2]) for row in rows]


async def clear_expired_comments(cutoff: datetime) -> int:
    """Обнулить комментарии старше `cutoff` (§4.5, spec 0032); оценка остаётся."""
    async with session_scope() as session:
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                update(RequestFeedback)
                .where(RequestFeedback.comment.is_not(None), RequestFeedback.comment_at < cutoff)
                .values(comment=None)
            ),
        )
    return result.rowcount or 0


async def _enabled_escalation_key() -> str:
    """Служба карточек; оценки выключены (или конфига нет) — ERR-FEEDBACK-001."""
    key = await _escalation_key_if_enabled()
    if key is None:
        raise AppError(code=ERR_FEEDBACK_DISABLED, message="Feedback is off", status_code=404)
    return key


async def _escalation_key_if_enabled() -> str | None:
    """Ключ службы эскалаций, если оценки включены; валидатор конфига держит пару."""
    try:
        async with session_scope() as session:
            config = await load_tenant_config(session, current_tenant_id())
    except AppError as error:
        if error.code != TENANT_NOT_CONFIGURED_ERROR_CODE:
            raise
        return None
    return config.escalation_category_key if config.guest_feedback_enabled else None


async def _rateable_request(
    request_id: uuid.UUID, stay_id: uuid.UUID
) -> requests_api.ServiceRequestRead:
    """Заявка `done` этого Stay; нет, не `done`, чужой Stay — ERR-FEEDBACK-002 (404)."""
    try:
        request = await requests_api.get_request(request_id)
    except AppError as error:
        if error.code != requests_api.ERR_REQUESTS_REQUEST_NOT_FOUND:
            raise
        request = None
    if (
        request is None
        or request.status is not requests_api.RequestStatus.DONE
        or request.stay_id != stay_id
    ):
        raise AppError(
            code=ERR_FEEDBACK_NOT_RATEABLE, message="Request cannot be rated", status_code=404
        )
    return request


async def _repeat(
    session: AsyncSession, request_id: uuid.UUID, rating: FeedbackRating
) -> FeedbackRead:
    """Оценка уже есть: та же — прежняя без новой карточки (P-8), другая — 409."""
    row = (
        await session.execute(
            select(
                RequestFeedback.rating,
                RequestFeedback.comment_at,
                RequestFeedback.card_request_id,
            ).where(RequestFeedback.request_id == request_id)
        )
    ).one()
    if row.rating is not rating:
        raise AppError(
            code=ERR_FEEDBACK_ALREADY_RATED,
            message="Request already rated differently",
            status_code=409,
        )
    logger.info("feedback_repeated", request_id=str(request_id), rating=rating.value)
    return FeedbackRead(
        request_id=request_id,
        rating=rating,
        commented=row.comment_at is not None,
        passed_to_staff=row.card_request_id is not None,
    )


async def _set_card(session: AsyncSession, feedback_id: uuid.UUID, card_id: uuid.UUID) -> None:
    await session.execute(
        update(RequestFeedback)
        .where(RequestFeedback.id == feedback_id)
        .values(card_request_id=card_id, updated_at=utc_now())
    )


async def _card_step(
    rated: requests_api.ServiceRequestRead, escalation_key: str, *, comment: str | None
) -> uuid.UUID | None:
    """Завести карточку пальца вниз; None — не вышло (лог уже записан)."""
    try:
        card_id = await cards.open_card(rated, escalation_key=escalation_key, comment=comment)
    except Exception as error:  # best-effort (§4.3): оценка записана, гостю — без «передали»
        _log_card_failed(rated.id, error)
        return None
    logger.info("feedback_card_created", request_id=str(rated.id), card_request_id=str(card_id))
    return card_id


async def _append_step(
    card_id: uuid.UUID,
    rated: requests_api.ServiceRequestRead,
    escalation_key: str,
    comment: str,
) -> bool | None:
    """Комментарий — в карточку: True — дописан, False — вторая карточка, None — сбой."""
    try:
        target, appended = await cards.append_comment(
            card_id, rated, escalation_key=escalation_key, comment=comment
        )
    except Exception as error:  # best-effort (§4.3)
        _log_card_failed(rated.id, error)
        return None
    if not appended:
        logger.info("feedback_card_created", request_id=str(rated.id), card_request_id=str(target))
    return appended


def _log_card_failed(request_id: uuid.UUID, error: Exception) -> None:
    """ERROR — до Sentry (§4.3). Текст исключения не пишется: `IntegrityError`
    несёт параметры SQL, а среди них — суть заявки и комментарий гостя (§4.5)."""
    logger.error(
        "feedback_card_failed",
        error_code=ERR_FEEDBACK_CARD_FAILED,
        cause=error.code if isinstance(error, AppError) else "unexpected",
        error_type=type(error).__name__,
        request_id=str(request_id),
    )
