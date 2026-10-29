"""HTTP-маршруты формы реквизитов для АВР (spec 0049 §5, Р-7…Р-9, PR 4 серии).

Всё — под `/g/{tenant_slug}/{room_number}`, как чат: контекст тенанта ставит
канал по slug, сессия — та же cookie (`router.require_guest_session`). Номер,
даты и личность — только из сессии и её проживания, `{room}` пути не
используется (канон чата). Приём выключен (выключатель контура или нет службы
«Бухгалтерия») — 404 `ERR-DOCUMENTS-005`: страницы нет.

Тело отправки читает только `platform/upload_form.read_upload_form` (CANONICAL):
потолок 51 МБ до разбора, файлы — в памяти. Отказы, которые маршрут ждёт, — это
`AppError`, их лог — WARNING: ERROR-лог на этом пути унёс бы в Sentry поля формы
(#433 открыт; ревью PR #500, Н-1).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any
from urllib.parse import quote as url_quote

from fastapi import APIRouter, Cookie, Depends, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import ValidationError

from hospitality.channels.web import service
from hospitality.channels.web.documents import messages, page, texts
from hospitality.channels.web.documents.hotel import (
    guest_documents_enabled,
    hotel_zone,
    photos_read_by_ai,
)
from hospitality.channels.web.documents.schemas import DocumentsData, DocumentsSent
from hospitality.channels.web.router import SESSION_COOKIE, require_guest_session
from hospitality.modules.documents import api as documents_api
from hospitality.modules.guests import api as guests_api
from hospitality.platform.upload_form import (
    UploadBodyTooLargeError,
    UploadForm,
    UploadFormError,
    UploadTooManyFilesError,
    read_upload_form,
)
from hospitality.shared.errors import (
    VALIDATION_ERROR_CODE,
    AppError,
    ErrorDetail,
    ErrorResponse,
)
from hospitality.shared.logging import get_logger
from hospitality.shared.middleware import get_correlation_id
from hospitality.shared.tenancy import tenant_context

logger = get_logger(module=__name__)

router = APIRouter(prefix="/g", tags=["web-chat"])

# Р-8: пять файлов по 10 МБ и текстовые поля.
MAX_BODY_BYTES = 51 * 1024 * 1024

GuestAuth = Annotated[
    tuple[uuid.UUID, guests_api.ActiveGuestSession], Depends(require_guest_session)
]
_DISABLED: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Приём выключен (ERR-DOCUMENTS-005)"}
}


@router.get(
    "/{tenant_slug}/{room_number}/documents",
    response_class=HTMLResponse,
    summary="Форма «Реквизиты для закрывающих документов» (spec 0049)",
    responses=_DISABLED,
)
async def documents_page(
    tenant_slug: str,
    room_number: str,
    guest_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> Response:
    """Без сессии — 303 на чат, где гость входит по коду."""
    tenant_id = await service.resolve_tenant(tenant_slug)
    with tenant_context(tenant_id):
        await _require_enabled()
        session = await service.resolve_session(guest_session)
        if session is None:
            return RedirectResponse(
                f"/g/{tenant_slug}/{url_quote(room_number, safe='')}", status_code=303
            )
        logger.info("act_request_form_opened")
    return HTMLResponse(page.render())


@router.get(
    "/{tenant_slug}/{room_number}/documents/data",
    summary="Номер и даты проживания для шапки формы",
    responses=_DISABLED,
)
async def documents_data(
    auth: GuestAuth, language: Annotated[str | None, Query(max_length=35)] = None
) -> DocumentsData:
    tenant_id, session = auth
    lang = texts.guest_language(language)
    with tenant_context(tenant_id):
        await _require_enabled()
        stay = await guests_api.get_active_stay(session.stay_id)
        zone = await hotel_zone()
    if stay is None:
        # Проживание погасили между проверкой сессии и чтением — как у чата: 401.
        raise await service.unauthenticated_error()
    check_in_at = stay.check_in_at
    return DocumentsData(
        language=lang,
        room_number=session.room_number,
        check_in_label=texts.day_label(check_in_at, zone, lang),
        check_out_label=texts.day_label(session.check_out_at, zone, lang),
        photos_read_by_ai=photos_read_by_ai(),
    )


@router.post(
    "/{tenant_slug}/{room_number}/documents",
    summary="Отправить реквизиты в бухгалтерию; повтор ключа — тот же запрос",
    responses={
        **_DISABLED,
        413: {
            "model": ErrorResponse,
            "description": "Больше 5 файлов или 51 МБ (002); файл (FILES-001)",
        },
        415: {"model": ErrorResponse, "description": "Файл не того формата (FILES-002, -003)"},
        422: {"model": ErrorResponse, "description": "Пустая форма (001) или поля формы"},
        429: {"model": ErrorResponse, "description": "Пятый запрос проживания уже был (003)"},
    },
)
async def submit_documents(request: Request, auth: GuestAuth) -> Response:
    tenant_id, session = auth
    with tenant_context(tenant_id):
        await _require_enabled()
        form = await _read_form(request)
        submit = _submit_body(form, session)
        files = [documents_api.UploadInput(filename=p.filename, data=p.data) for p in form.files]
        try:
            act = await documents_api.submit_act_request(submit, files)
        except documents_api.ActRequestFileError as error:
            return _file_error(request, error)
        try:
            await messages.notify_received(act)
        except Exception:
            # Запрос уже принят: без карточки в чате гость всё равно видит экран
            # «Отправлено». WARNING, а не ERROR, — см. докстринг модуля.
            logger.warning("act_request_received_message_failed", exc_info=True)
        zone = await hotel_zone()
    sent = DocumentsSent(
        files_count=act.files_count,
        check_out_label=texts.day_label(act.check_out_at, zone, act.guest_language),
    )
    return JSONResponse(sent.model_dump())


async def _require_enabled() -> None:
    if not await guest_documents_enabled():
        raise AppError(
            code=documents_api.ERR_DOCUMENTS_DISABLED,
            message="Document upload is turned off",
            status_code=404,
        )


async def _read_form(request: Request) -> UploadForm:
    try:
        return await read_upload_form(
            request, max_body_bytes=MAX_BODY_BYTES, max_files=documents_api.MAX_FILES
        )
    except (UploadBodyTooLargeError, UploadTooManyFilesError) as exc:
        raise AppError(
            code=documents_api.ERR_DOCUMENTS_TOO_MANY_FILES,
            message="Too many files or the form is too large",
            status_code=413,
        ) from exc
    except UploadFormError as exc:
        raise AppError(
            code=VALIDATION_ERROR_CODE, message="The form could not be read", status_code=422
        ) from exc


def _submit_body(
    form: UploadForm, session: guests_api.ActiveGuestSession
) -> documents_api.ActRequestSubmit:
    """Поля формы → схема домена; проживание и личность — из сессии, язык — Р-9."""
    try:
        return documents_api.ActRequestSubmit.model_validate(
            {
                "stay_id": session.stay_id,
                "guest_identity_id": session.guest_identity_id,
                "client_key": form.fields.get("client_key", ""),
                "language": texts.guest_language(form.fields.get("language")),
                "bill_to": form.fields.get("bill_to", ""),
                "note": form.fields.get("note", ""),
            }
        )
    except ValidationError as exc:
        # Канонический 422 (`shared/errors.py`): сырой ввод в лог не пишется.
        raise RequestValidationError(exc.errors()) from exc


def _file_error(request: Request, error: documents_api.ActRequestFileError) -> JSONResponse:
    """Отказ узла по файлу: код узла и номер файла — страница назовёт его по имени (§6)."""
    logger.warning(
        "act_request_file_rejected",
        code=error.code,
        status_code=error.status_code,
        file_position=error.position,
    )
    envelope = ErrorResponse(
        error=ErrorDetail(
            code=error.code,
            message=error.message,
            correlation_id=get_correlation_id(request),
            details=[{"file_position": error.position}],
        )
    )
    return JSONResponse(envelope.model_dump(exclude_none=True), status_code=error.status_code)
