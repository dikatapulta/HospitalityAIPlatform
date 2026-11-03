"""Действия страницы «Вид гостевого чата» (spec 0066 §6; issue #761).

Канон `catalog_api.py`: CSRF-щит (`browser.require_json_same_origin`, загрузка —
`require_upload_same_origin`), личность — `staff_auth.require_role` менеджера, исходы —
конверт ошибок R-8. Тело загрузки знака читает только `platform/upload_form.read_upload_form`
(CANONICAL) и только после права: чужой запрос тело не тратит. Действия зовёт
`static/chat_look.js`.

Предпросмотр — HTML страницы гостя в iframe самой страницы кабинета: свои заголовки
(`PREVIEW_HEADERS`), потому что стили гостя встроены в страницу, а узор — `data:`-картинка,
которых CSP кабинета не пускает. Скрипта в предпросмотре нет, и CSP его запрещает.
"""

from __future__ import annotations

from typing import Final, Literal

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict

from hospitality.platform import staff_auth
from hospitality.platform.file_content import max_upload_bytes
from hospitality.platform.files import ERR_FILE_TOO_LARGE, FilePurpose
from hospitality.platform.guest_chat_look import GuestChatPalette, GuestChatPattern
from hospitality.platform.models import StaffRole
from hospitality.platform.upload_form import (
    UploadBodyTooLargeError,
    UploadedPart,
    UploadForm,
    UploadFormError,
    read_upload_form,
)
from hospitality.shared.errors import VALIDATION_ERROR_CODE, AppError
from hospitality.staff_portal import chat_look, queue
from hospitality.staff_portal.browser import require_json_same_origin, require_upload_same_origin

router = APIRouter(prefix="/{tenant_slug}/api/chat-look", tags=["staff-portal"])

_manager_role: Final = staff_auth.require_role(StaffRole.MANAGER)
# Тело загрузки знака: потолок файла узла (5 МБ) и запас на разметку multipart.
_LOGO_BODY_BYTES: Final = max_upload_bytes(FilePurpose.HOTEL_LOGO) + 64 * 1024

# Предпросмотр: стили — только встроенные, картинки — свои и `data:` (узор, favicon),
# скрипты и прочее закрыты; встраивать — только своим страницам (iframe кабинета).
PREVIEW_HEADERS: Final[dict[str, str]] = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "SAMEORIGIN",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
        "frame-ancestors 'self'"
    ),
    "Referrer-Policy": "strict-origin",
}


class ChoiceBody(BaseModel):
    """«Сохранить»: вариант подложки и узор; неизвестное значение — 422 схемы."""

    model_config = ConfigDict(extra="forbid")

    palette: GuestChatPalette
    pattern: GuestChatPattern


async def _manager(request: Request, *, upload: bool = False) -> staff_auth.StaffContext:
    """CSRF и личность — до всего."""
    if upload:
        require_upload_same_origin(request)
    else:
        require_json_same_origin(request)
    return await _manager_role(request)


@router.post("", summary="Вид гостевого чата: сохранить цвет подложки и узор")
async def save_choice(request: Request, body: ChoiceBody) -> dict[str, bool]:
    staff = await _manager(request)
    await chat_look.save_choice(staff, body.palette, body.pattern)
    return {"ok": True}


@router.post("/logo", summary="Знак отеля: загрузить или заменить")
async def set_logo(request: Request) -> dict[str, bool]:
    """Один файл в поле `logo`; проверку, перекодировку и отказы 001–003 даёт узел файлов."""
    staff = await _manager(request, upload=True)
    form = await _read_logo_form(request)
    await chat_look.set_logo(staff, form.data, form.filename)
    return {"ok": True}


@router.post("/logo/remove", summary="Знак отеля: убрать")
async def remove_logo(request: Request) -> dict[str, bool]:
    staff = await _manager(request)
    await chat_look.remove_logo(staff)
    return {"ok": True}


@router.get("/preview", response_class=HTMLResponse, summary="Телефон предпросмотра")
async def preview(
    request: Request,
    palette: GuestChatPalette,
    pattern: GuestChatPattern,
    theme: Literal["day", "night"],
) -> HTMLResponse:
    """Выбор со страницы (ещё не сохранён) поверх сохранённого знака; только менеджер."""
    staff = await _manager_role(request)
    config = await queue.tenant_config(staff)
    content = chat_look.preview_html(
        staff, config, palette=palette, pattern=pattern, night=theme == "night"
    )
    return HTMLResponse(content, headers=PREVIEW_HEADERS)


async def _read_logo_form(request: Request) -> UploadedPart:
    try:
        form: UploadForm = await read_upload_form(
            request, max_body_bytes=_LOGO_BODY_BYTES, max_files=1
        )
    except UploadBodyTooLargeError as exc:
        raise AppError(
            code=ERR_FILE_TOO_LARGE,
            message="Файл слишком большой. Фото — до 5 МБ, документ — до 10 МБ.",
            status_code=413,
        ) from exc
    except UploadFormError as exc:
        raise AppError(
            code=VALIDATION_ERROR_CODE, message="The form could not be read", status_code=422
        ) from exc
    if len(form.files) != 1:
        raise AppError(
            code=VALIDATION_ERROR_CODE, message="Exactly one picture is expected", status_code=422
        )
    return form.files[0]
