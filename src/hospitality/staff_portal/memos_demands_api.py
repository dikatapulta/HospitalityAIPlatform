"""Действия с требованием объяснения (spec 0077 §6, §9; #936, PR 5б).

«Затребовать объяснение» — загрузка фото подписанного требования: канон `catalog_api.py`
(CSRF-щит загрузки `browser.require_upload_same_origin`, тело — только
`platform/upload_form.read_upload_form` и только после права: `staff` и суперадмин тело
не тратят). Кого можно — модуль (`create_demand`: `manager` — у любого, `senior` — у своих
служб, иначе ERR-PLATFORM-002 `not_your_service`); файл — `platform/files.py` назначением
`memo_demand` (ERR-FILES-001…003). После записи — пуш сотруднику (§9, без имён).

«Получил(а)» — окно подтверждения, как подпись в листе (`memo_confirm.js` зовёт
`…/demands/{id}/begin` и `…/complete`): `begin_receipt` / `confirm_receipt` модуля —
только сотруднику, один раз, с образцом подписи. В логах — только id.
"""

from __future__ import annotations

import uuid
from typing import Final, Literal, Self

from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from hospitality.modules.memos import api as memos_api
from hospitality.platform import staff_auth
from hospitality.platform.file_content import max_upload_bytes
from hospitality.platform.files import ERR_FILE_TOO_LARGE, FilePurpose
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import StaffContext
from hospitality.platform.upload_form import (
    UploadBodyTooLargeError,
    UploadForm,
    UploadFormError,
    read_upload_form,
)
from hospitality.shared.clientip import client_ip
from hospitality.shared.errors import VALIDATION_ERROR_CODE, AppError
from hospitality.staff_portal import browser, memos, memos_push
from hospitality.staff_portal import memos_texts as texts
from hospitality.staff_portal.browser import require_json_same_origin, require_upload_same_origin
from hospitality.staff_portal.memos_people import can_demand

router = APIRouter(prefix="/{tenant_slug}/api/memos/demands", tags=["staff-portal"])

_demander_role: Final = staff_auth.require_role(StaffRole.SENIOR, StaffRole.MANAGER)
# Тело загрузки: потолок фото узла (5 МБ) и запас на разметку multipart.
_DEMAND_BODY_BYTES: Final = max_upload_bytes(FilePurpose.MEMO_DEMAND) + 64 * 1024
_PASSWORD_MAX_LENGTH = 256
_CREDENTIAL_MAX_LENGTH = 16_384


class ReceiveIntent(BaseModel):
    """«Получил(а)» — единственное действие под именем у требования."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["receive"] = "receive"


class BeginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: ReceiveIntent


class CompleteBody(BeginBody):
    """Ответ окна подтверждения: ключ устройства (`credential`) или пароль — одно из двух."""

    challenge_id: str = Field(min_length=1, max_length=128)
    credential: str | None = Field(default=None, min_length=1, max_length=_CREDENTIAL_MAX_LENGTH)
    password: str | None = Field(default=None, min_length=1, max_length=_PASSWORD_MAX_LENGTH)

    @model_validator(mode="after")
    def _one_answer(self) -> Self:
        if (self.credential is None) == (self.password is None):
            raise ValueError("send either credential or password")
        return self


def _forbidden() -> AppError:
    return AppError(
        code=staff_auth.ERR_AUTH_FORBIDDEN,
        message="Only a senior or a manager can demand an explanation",
        status_code=403,
    )


async def _demander(request: Request) -> StaffContext:
    """CSRF загрузки и право — до чтения тела; раздел выключен у отеля — 404."""
    require_upload_same_origin(request)
    staff = await _demander_role(request)
    await memos.require_section(staff)
    if not can_demand(staff):  # суперадмин проходит `require_role` своим членством
        raise _forbidden()
    return staff


async def _read_demand_form(request: Request) -> tuple[uuid.UUID, str, bytes]:
    try:
        form: UploadForm = await read_upload_form(
            request, max_body_bytes=_DEMAND_BODY_BYTES, max_files=1
        )
    except UploadBodyTooLargeError as exc:
        raise AppError(
            code=ERR_FILE_TOO_LARGE,
            message="Файл слишком большой. Фото — до 5 МБ.",
            status_code=413,
        ) from exc
    except UploadFormError as exc:
        raise AppError(
            code=VALIDATION_ERROR_CODE, message="The form could not be read", status_code=422
        ) from exc
    try:
        employee_id = uuid.UUID(form.fields.get("employee_id", ""))
    except ValueError as exc:
        raise AppError(
            code=VALIDATION_ERROR_CODE,
            message="Pick an employee",
            status_code=422,
            details=[{"field": "employee_id", "reason": "required"}],
        ) from exc
    if len(form.files) != 1:
        raise AppError(
            code=VALIDATION_ERROR_CODE, message="Exactly one photo is expected", status_code=422
        )
    return employee_id, form.files[0].filename, form.files[0].data


@router.post("", summary="Затребовать объяснение: сотрудник и фото требования")
async def create_demand(request: Request, background: BackgroundTasks) -> dict[str, str]:
    staff = await _demander(request)
    employee_id, filename, data = await _read_demand_form(request)
    demand_id = await memos_api.create_demand(staff, employee_id, data, filename)
    href = memos.demand_href(staff, demand_id)
    background.add_task(
        memos_push.notify, staff.tenant_id, demand_id, [employee_id], texts.PUSH_DEMAND, url=href
    )
    return {"id": str(demand_id), "href": href}


async def _actor(request: Request) -> StaffContext:
    require_json_same_origin(request)
    staff = await memos.any_role(request)
    await memos.require_section(staff)
    return staff


@router.post("/{demand_id:uuid}/begin", summary="«Получил(а)»: окно подтверждения")
async def begin(request: Request, demand_id: uuid.UUID, body: BeginBody) -> dict[str, str | None]:
    del body  # действие одно — «Получил(а)»
    staff = await _actor(request)
    started = await memos_api.begin_receipt(staff, demand_id)
    return {"challenge_id": started.challenge_id, "options": started.options_json}


@router.post("/{demand_id:uuid}/complete", summary="«Получил(а)»: ответ ключа или пароль")
async def complete(request: Request, demand_id: uuid.UUID, body: CompleteBody) -> dict[str, str]:
    staff = await _actor(request)
    answer: memos_api.PasskeyAnswer | memos_api.PasswordAnswer
    if body.credential is not None:
        answer = memos_api.PasskeyAnswer(
            challenge_id=body.challenge_id, credential_json=body.credential
        )
    else:
        assert body.password is not None  # валидатор: ровно одно из двух
        answer = memos_api.PasswordAnswer(
            challenge_id=body.challenge_id,
            password=body.password,
            client_ip=client_ip(request),
            device_token=request.cookies.get(browser.STAFF_DEVICE_COOKIE),
        )
    due_at = await memos_api.confirm_receipt(staff, demand_id, answer)
    return {"status": "received", "due_at": due_at.isoformat()}
