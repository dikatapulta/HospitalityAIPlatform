"""HTTP-маршруты фото гостя к заявке (spec 0081 §4, PR 2 серии #82).

Скрипт фото `/g/static/photo.js` (`widget.py`) — общий для всех отелей, без сессии;
поэтому роутер подключён раньше чата. Остальное — под `/g/{tenant_slug}/{room_number}`,
как чат: контекст тенанта ставит
канал по slug, сессия — та же cookie (`router.require_guest_session`); проживание
и личность — только из сессии. Фото выключены (выключатель контура или режим
«только консультации») — 404 `ERR-WEB-006`: маршрутов нет.

Порядок отправки (§4): выключатель → лимит проживания → форма → повтор ключа →
узел файлов (`store_upload`, CANONICAL) → входящее фото → правило §3 → ответ гостю.
Тело читает только `platform/upload_form.read_upload_form`: потолок до разбора,
файл — в памяти. Отказы узла — `AppError` с WARNING-логом: ERROR-лог на этом пути
унёс бы в Sentry тело запроса, то есть фото (#433).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

from hospitality.channels.common.guest_photo import (
    find_inbound_photo,
    insert_inbound_photo,
    is_conversation_photo,
    receive_guest_photo,
)
from hospitality.channels.common.models import Message
from hospitality.channels.common.store import ensure_conversation
from hospitality.channels.web.photos import widget
from hospitality.channels.web.photos.hotel import guest_photos_enabled, photo_base
from hospitality.channels.web.router import require_guest_session
from hospitality.channels.web.schemas import ChatMessage, SendPhotoResult
from hospitality.channels.web.service import CHANNEL
from hospitality.modules.guests import api as guests_api
from hospitality.platform.files import (
    ERR_FILE_NOT_FOUND,
    ERR_FILE_TOO_LARGE,
    FilePurpose,
    delete_file,
    file_response,
    read_file,
    store_upload,
)
from hospitality.platform.upload_form import (
    UploadBodyTooLargeError,
    UploadForm,
    UploadFormError,
    read_upload_form,
)
from hospitality.shared.errors import VALIDATION_ERROR_CODE, AppError, ErrorResponse
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_guest_rate_limited
from hospitality.shared.middleware import get_correlation_id
from hospitality.shared.ratelimit import consume_rate_limit
from hospitality.shared.tenancy import tenant_context

logger = get_logger(module=__name__)

router = APIRouter(prefix="/g", tags=["web-chat"])

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_WEB_PHOTOS_DISABLED: Final = "ERR-WEB-006"
ERR_WEB_PHOTO_RATE_LIMITED: Final = "ERR-WEB-007"

# Одно фото — до 5 МБ у узла (`IMAGE_MAX_BYTES`), страница больше не шлёт: уменьшает
# снимок сама. Запас — на разметку формы и поля.
MAX_BODY_BYTES: Final = 6 * 1024 * 1024
# Лимит проживания (spec 0081 §4): 10 фото за час — гостю с одной поломкой хватит
# с запасом, а заполнить хранилище отеля снимками подряд не выйдет. Окно — канон
# `shared/ratelimit.py` (fixed window).
PHOTO_RATE_LIMIT_SCOPE: Final = "guest_photo"
PHOTOS_PER_WINDOW: Final = 10
PHOTO_RATE_WINDOW_SECONDS: Final = 3600

GuestAuth = Annotated[
    tuple[uuid.UUID, guests_api.ActiveGuestSession], Depends(require_guest_session)
]
_DISABLED: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Фото выключены (ERR-WEB-006)"}
}


@router.get(widget.PATH.removeprefix(router.prefix), include_in_schema=False)
async def photo_script() -> Response:
    """Без тенанта и сессии, кэш навсегда — версию меняет адрес (`widget.URL`), как у оценки."""
    return Response(
        widget.SCRIPT,
        media_type="text/javascript; charset=utf-8",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": f'"{widget.VERSION}"',
        },
    )


class _PhotoFields(BaseModel):
    """Поля формы фото: ключ отправки (повтор не плодит файлов) и язык страницы."""

    client_key: uuid.UUID
    language: Literal["kk", "ru", "en"]


@router.post(
    "/{tenant_slug}/{room_number}/photos",
    summary="Фото гостя к заявке; повтор ключа — то же фото",
    responses={
        **_DISABLED,
        413: {"model": ErrorResponse, "description": "Больше 5 МБ (FILES-001)"},
        415: {"model": ErrorResponse, "description": "Не фото (FILES-002)"},
        422: {"model": ErrorResponse, "description": "Форма: нет файла, ключа или языка"},
        429: {"model": ErrorResponse, "description": "10 фото за час уже было (ERR-WEB-007)"},
        503: {"model": ErrorResponse, "description": "Хранилище недоступно (FILES-005, -006)"},
    },
)
async def send_photo(tenant_slug: str, request: Request, auth: GuestAuth) -> SendPhotoResult:
    tenant_id, session = auth
    correlation_id = get_correlation_id(request) or ""
    base = photo_base(tenant_slug, session.room_number)
    with tenant_context(tenant_id):
        if base is None:
            raise _disabled()
        await _consume_photo_limit(tenant_id, session.stay_id)
        form = await _read_form(request)
        fields = _fields(form)
        conversation_id = await ensure_conversation(
            CHANNEL, str(session.guest_identity_id), guest_identity_id=session.guest_identity_id
        )
        key = f"web:photo:{fields.client_key}"
        existing = await find_inbound_photo(conversation_id, key)
        if existing is not None:
            logger.info("web_duplicate_photo", client_key=str(fields.client_key))
            return _duplicate(existing, base)
        part = form.files[0]
        try:
            stored = await store_upload(
                FilePurpose.REQUEST_PHOTO,
                part.data,
                part.filename,
                guest_identity_id=session.guest_identity_id,
            )
        except AppError as error:
            # 001 и 002 — гость прислал не то; 005 и 006 — сбой у нас (лог узла — выше).
            logger.warning(
                "guest_photo_rejected", error_code=error.code, status_code=error.status_code
            )
            raise
        message = await insert_inbound_photo(
            conversation_id,
            stored.id,
            idempotency_key=key,
            external_message_id=str(fields.client_key),
            correlation_id=correlation_id,
        )
        if message is None:
            # Гонку двух повторов выиграл другой: свой файл — прочь (P-8).
            await delete_file(stored.id)
            existing = await find_inbound_photo(conversation_id, key)
            if (
                existing is None
            ):  # pragma: no cover — ключ занят чужим диалогом: UUID не повторяется
                raise AppError(code=VALIDATION_ERROR_CODE, message="Key taken", status_code=422)
            return _duplicate(existing, base)
        reply = await receive_guest_photo(
            conversation_id, stored.id, language=fields.language, correlation_id=correlation_id
        )
    return SendPhotoResult(
        replies=[reply.text],
        reply_ids=[reply.message_id],
        last_message_id=reply.message_id,
        photo=_photo_message(message, base),
    )


@router.get(
    "/{tenant_slug}/{room_number}/photos/{file_id}",
    summary="Своё фото гостя в ленте чата",
    responses={
        **_DISABLED,
        404: {"model": ErrorResponse, "description": "Не фото этого диалога (ERR-FILES-004)"},
    },
)
async def guest_photo(tenant_slug: str, file_id: uuid.UUID, auth: GuestAuth) -> Response:
    """Отдаётся только файл входящего фото диалога этой сессии; чужое — как несуществующее."""
    tenant_id, session = auth
    if not guest_photos_enabled():
        raise _disabled()
    with tenant_context(tenant_id):
        conversation_id = await ensure_conversation(
            CHANNEL, str(session.guest_identity_id), guest_identity_id=session.guest_identity_id
        )
        if not await is_conversation_photo(conversation_id, file_id):
            logger.info("guest_photo_not_found", file_id=str(file_id))
            raise AppError(code=ERR_FILE_NOT_FOUND, message="Файл не найден.", status_code=404)
        stored = await read_file(file_id)
    return file_response(stored)


def _disabled() -> AppError:
    return AppError(code=ERR_WEB_PHOTOS_DISABLED, message="Photos are turned off", status_code=404)


async def _consume_photo_limit(tenant_id: uuid.UUID, stay_id: uuid.UUID) -> None:
    """10 фото проживания за окно; Redis недоступен — пропуск, как у лимитов чата."""
    decision = await consume_rate_limit(
        PHOTO_RATE_LIMIT_SCOPE,
        f"{tenant_id}:{stay_id}",
        limit=PHOTOS_PER_WINDOW,
        window_seconds=PHOTO_RATE_WINDOW_SECONDS,
    )
    if decision.available and not decision.allowed:
        logger.warning(
            "guest_photo_rate_limited",
            error_code=ERR_WEB_PHOTO_RATE_LIMITED,
            count=decision.count,
            limit=decision.limit,
        )
        record_guest_rate_limited(PHOTO_RATE_LIMIT_SCOPE)
        raise AppError(
            code=ERR_WEB_PHOTO_RATE_LIMITED,
            message="Too many photos in a row",
            status_code=429,
        )


async def _read_form(request: Request) -> UploadForm:
    try:
        form = await read_upload_form(request, max_body_bytes=MAX_BODY_BYTES, max_files=1)
    except UploadBodyTooLargeError as exc:
        raise AppError(
            code=ERR_FILE_TOO_LARGE, message="The photo is too large", status_code=413
        ) from exc
    except UploadFormError as exc:
        raise AppError(
            code=VALIDATION_ERROR_CODE, message="The form could not be read", status_code=422
        ) from exc
    if len(form.files) != 1:
        raise AppError(code=VALIDATION_ERROR_CODE, message="One photo expected", status_code=422)
    return form


def _fields(form: UploadForm) -> _PhotoFields:
    try:
        return _PhotoFields.model_validate(
            {
                "client_key": form.fields.get("client_key", ""),
                "language": form.fields.get("language", ""),
            }
        )
    except ValidationError as exc:
        # Канонический 422 (`shared/errors.py`): сырой ввод в лог не пишется.
        raise RequestValidationError(exc.errors()) from exc


def _photo_message(message: Message, base: str) -> ChatMessage:
    return ChatMessage(
        id=message.id,
        direction="inbound",
        text="",
        created_at=message.created_at,
        client_message_id=message.external_message_id,
        photo_url=f"{base}/{message.file_id}" if message.file_id else None,
    )


def _duplicate(message: Message, base: str) -> SendPhotoResult:
    """Повтор ключа: второго файла и второго ответа нет — ответ дозаберёт опрос."""
    return SendPhotoResult(
        replies=[], duplicate=True, last_message_id=message.id, photo=_photo_message(message, base)
    )
