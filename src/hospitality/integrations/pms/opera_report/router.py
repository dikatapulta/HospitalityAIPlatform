"""Маршруты приёма выгрузок Opera (spec 0058 §2.4, spec 0059 §2.1, ADR-004 транспорт «а»).

`POST /integrations/pms/{tenant_slug}/opera-report` — снимок «кто в доме»,
`POST /integrations/pms/{tenant_slug}/opera-availability` — свободные номера по
ночам; `Authorization: Bearer <токен загрузки>` у обоих один, тело — файл отчёта
как есть. Шлют их две задачи планировщика Windows на компьютере отеля (рунбук
`docs/runbooks/opera-report-upload.md`). Путь проверок у файлов общий, а счётчик
частоты и метрика — свои (`_Feed`): частый второй файл не съест лимит снимка.

Аутентифицируется не сервисным токеном и не сессией, а токеном загрузки
тенанта (ADR-008: в БД хэш); контекст тенанта ставит сам по slug, как веб-чат.
Порядок проверок дешёвый к дорогому, тело читается последним:
1. slug и токен → 401 `ERR-PMS-002` (неизвестный отель неотличим от чужого токена);
2. частота → 429 `ERR-PMS-004`: в счёт идут только запросы с верным токеном —
   чужой, знающий slug, выгрузку отеля не задержит;
3. приём у отеля включён (`pms_source: opera_report`) → иначе 409 `ERR-PMS-005`:
   номера броней не ложатся в базу до допсоглашения к договору (spec 0058 §4),
   а IT по коду 409 видит, что задача и токен уже работают;
4. размер → 413 `ERR-PMS-003`; разбор → 400 `ERR-PMS-001`.
В лог — тенант, файл (`feed`), исход, число строк, класс причины; номеров броней
и комнат нет.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Final, Literal, NoReturn

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel
from sqlalchemy import select

from hospitality.integrations.pms.opera_report.adapter import (
    OperaReportPmsAdapter,
    UploadResult,
    upload_token_matches,
)
from hospitality.integrations.pms.opera_report.parse import ReportRejectedError
from hospitality.platform.config import TENANT_NOT_CONFIGURED_ERROR_CODE, load_tenant_config
from hospitality.platform.models import Tenant
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError, ErrorResponse
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_pms_availability_upload, record_pms_report_upload
from hospitality.shared.ratelimit import consume_rate_limit
from hospitality.shared.tenancy import tenant_context

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_PMS_REPORT_INVALID = "ERR-PMS-001"
ERR_PMS_UNAUTHORIZED = "ERR-PMS-002"
ERR_PMS_TOO_LARGE = "ERR-PMS-003"
ERR_PMS_RATE_LIMITED = "ERR-PMS-004"
ERR_PMS_UPLOAD_DISABLED = "ERR-PMS-005"

MAX_REPORT_BYTES: Final = 1024 * 1024  # 1 МБ: отчёт на 350 номеров — десятки КБ
# Снимок раз в 3–10 минут — до 20 в час; свободные номера раз в 5–30 — до 12; запас на повторы.
UPLOADS_PER_HOUR: Final = 30
_ERROR_RESPONSES: Final[dict[int | str, dict[str, object]]] = {
    status: {"model": ErrorResponse} for status in (400, 401, 409, 413, 429)
}


@dataclass(frozen=True)
class _Feed:
    """Один из файлов отеля: свой счётчик частоты (`rate_scope`), своя метрика.

    `unchanged` — что остаётся в силе при отказе файлу: для текста ответа IT.
    """

    name: Literal["in_house", "availability"]
    rate_scope: str
    record: Callable[[str], None]
    unchanged: str


_IN_HOUSE: Final = _Feed("in_house", "pms_report_upload", record_pms_report_upload, "the snapshot")
_AVAILABILITY: Final = _Feed(
    "availability",
    "pms_availability_upload",
    record_pms_availability_upload,
    "the free-room figures",
)


class UploadAck(BaseModel):
    """`{"accepted": true, "rows": N}` или повтор `{"accepted": false, "duplicate": true}`."""

    accepted: bool
    rows: int | None = None
    duplicate: bool | None = None


class AvailabilityAck(BaseModel):
    """`{"accepted": true, "nights": N}` или повтор `{"accepted": false, "duplicate": true}`."""

    accepted: bool
    nights: int | None = None
    duplicate: bool | None = None


def build_upload_router(adapter: OperaReportPmsAdapter) -> APIRouter:
    """Маршруты загрузки, привязанные к адаптеру процесса; монтирует composition root."""
    router = APIRouter(prefix="/integrations/pms", tags=["pms"])

    @router.post(
        "/{tenant_slug}/opera-report",
        response_model=UploadAck,
        response_model_exclude_none=True,
        summary="Приём выгрузки отчёта Opera «in-house»",
        responses=_ERROR_RESPONSES,
    )
    async def upload_opera_report(
        tenant_slug: str,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> UploadAck:
        result = await _accept(
            _IN_HOUSE, adapter.accept_upload, tenant_slug, request, authorization
        )
        if not result.accepted:
            return UploadAck(accepted=False, duplicate=True)
        return UploadAck(accepted=True, rows=result.rows)

    @router.post(
        "/{tenant_slug}/opera-availability",
        response_model=AvailabilityAck,
        response_model_exclude_none=True,
        summary="Приём отчёта Opera «свободные номера по ночам»",
        responses=_ERROR_RESPONSES,
    )
    async def upload_opera_availability(
        tenant_slug: str,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> AvailabilityAck:
        result = await _accept(
            _AVAILABILITY, adapter.accept_availability, tenant_slug, request, authorization
        )
        if not result.accepted:
            return AvailabilityAck(accepted=False, duplicate=True)
        return AvailabilityAck(accepted=True, nights=result.rows)

    return router


async def _accept(
    feed: _Feed,
    accept: Callable[[uuid.UUID, bytes], Awaitable[UploadResult]],
    tenant_slug: str,
    request: Request,
    authorization: str | None,
) -> UploadResult:
    """Общий путь файла: токен → частота → приём включён → размер → разбор (докстринг модуля)."""
    tenant_id = await _authenticated_tenant(feed, tenant_slug, authorization)
    with tenant_context(tenant_id):
        await _within_rate_limit(feed, tenant_id)
        await _require_upload_enabled(feed, tenant_id)
        body = await _read_capped(feed, request)
        try:
            result = await accept(tenant_id, body)
        except ReportRejectedError as rejected:
            feed.record("rejected")
            logger.warning(
                "pms_report_rejected",
                error_code=ERR_PMS_REPORT_INVALID,
                feed=feed.name,
                reason=rejected.reason,
                line=rejected.line,
            )
            raise AppError(
                code=ERR_PMS_REPORT_INVALID,
                message=f"Report rejected, {feed.unchanged} unchanged: {rejected}",
                status_code=400,
            ) from None
        outcome = "accepted" if result.accepted else "duplicate"
        feed.record(outcome)
        logger.info("pms_report_uploaded", feed=feed.name, outcome=outcome, rows=result.rows)
    return result


async def _authenticated_tenant(
    feed: _Feed, tenant_slug: str, authorization: str | None
) -> uuid.UUID:
    scheme, _, token = (authorization or "").partition(" ")
    token = token.strip()
    tenant_id: uuid.UUID | None = None
    if scheme.lower() == "bearer" and token:
        async with platform_session_scope() as session:
            tenant_id = await session.scalar(select(Tenant.id).where(Tenant.slug == tenant_slug))
    if tenant_id is None or not await upload_token_matches(tenant_id, token):
        feed.record("unauthorized")
        logger.warning("pms_report_unauthorized", error_code=ERR_PMS_UNAUTHORIZED, feed=feed.name)
        raise AppError(
            code=ERR_PMS_UNAUTHORIZED,
            message="Missing or invalid upload token",
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return tenant_id


async def _within_rate_limit(feed: _Feed, tenant_id: uuid.UUID) -> None:
    decision = await consume_rate_limit(
        feed.rate_scope, str(tenant_id), limit=UPLOADS_PER_HOUR, window_seconds=3600
    )
    if decision.allowed:
        return
    feed.record("rate_limited")
    if decision.first_rejection:
        logger.warning("pms_report_rate_limited", error_code=ERR_PMS_RATE_LIMITED, feed=feed.name)
    raise AppError(
        code=ERR_PMS_RATE_LIMITED,
        message=f"More than {UPLOADS_PER_HOUR} uploads in an hour; try again later",
        status_code=429,
    )


async def _require_upload_enabled(feed: _Feed, tenant_id: uuid.UUID) -> None:
    try:
        async with platform_session_scope() as session:
            config = await load_tenant_config(session, tenant_id)
        enabled = config.pms_source == "opera_report"
    except AppError as error:
        if error.code != TENANT_NOT_CONFIGURED_ERROR_CODE:
            raise
        enabled = False
    if enabled:
        return
    feed.record("disabled")
    logger.info("pms_report_upload_disabled", error_code=ERR_PMS_UPLOAD_DISABLED, feed=feed.name)
    raise AppError(
        code=ERR_PMS_UPLOAD_DISABLED,
        message="The token works, but the hotel has not switched on Opera uploads yet; "
        "the file was not stored",
        status_code=409,
    )


async def _read_capped(feed: _Feed, request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_REPORT_BYTES:
        _too_large(feed)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_REPORT_BYTES:
            _too_large(feed)
    return bytes(body)


def _too_large(feed: _Feed) -> NoReturn:
    feed.record("too_large")
    logger.warning("pms_report_too_large", error_code=ERR_PMS_TOO_LARGE, feed=feed.name)
    raise AppError(
        code=ERR_PMS_TOO_LARGE,
        message=f"The file is larger than {MAX_REPORT_BYTES // 1024} KB",
        status_code=413,
    )
