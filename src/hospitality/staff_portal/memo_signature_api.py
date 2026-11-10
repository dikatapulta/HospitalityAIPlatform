"""Действия вкладки «Моя подпись» (spec 0077 §4; #936, PR 4б-1): привязать это устройство,
отвязать своё, сохранить образец подписи.

Канон `ideas_api.py`: CSRF-щит `browser.require_json_same_origin`, личность —
`staff_auth.require_role` любого уровня, раздел выключен у отеля — 404. Привязка — два
шага, как церемония WebAuthn в браузере: `bind/begin` (пароль кабинета → параметры для
`navigator.credentials.create`) и `bind/finish` (ответ ключа → запись). Название
устройства сервер выводит сам из заголовка `User-Agent` запроса (аудит #949, В2-7) —
с клиента оно не приходит. Отказы — каталогом ошибок платформы: ERR-AUTH-017 (пароль),
ERR-AUTH-006 (бюджет попыток входа), ERR-AUTH-018 (привязка: не запомненный вход,
суперадмин, пять устройств, ответ ключа не прошёл). В логах — только id.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from hospitality.modules.memos import api as memos_api
from hospitality.platform import staff_passkey_binding, staff_passkeys
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.clientip import client_ip
from hospitality.staff_portal import browser, memos
from hospitality.staff_portal.browser import require_json_same_origin

router = APIRouter(prefix="/{tenant_slug}/api/memos/signature", tags=["staff-portal"])

# Пароль кабинета — тот же предел, что у формы входа браузера (без своей политики здесь).
_PASSWORD_MAX_LENGTH = 256


class PasswordBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=_PASSWORD_MAX_LENGTH)


class FinishBody(BaseModel):
    """Ответ `navigator.credentials.create`, сериализованный в JSON браузером."""

    model_config = ConfigDict(extra="forbid")

    credential: str = Field(
        min_length=1, max_length=staff_passkey_binding.MAX_CREDENTIAL_JSON_LENGTH
    )


async def _actor(request: Request) -> StaffContext:
    """CSRF и личность — до всего; затем раздел у отеля: выключен — 404."""
    require_json_same_origin(request)
    staff = await memos.any_role(request)
    await memos.require_section(staff)
    return staff


def _device_token(request: Request) -> str | None:
    return request.cookies.get(browser.STAFF_DEVICE_COOKIE)


@router.post("/bind/begin", summary="Привязать это устройство: пароль → параметры ключа")
async def bind_begin(request: Request, body: PasswordBody) -> dict[str, str]:
    staff = await _actor(request)
    options = await staff_passkey_binding.begin_binding(
        staff,
        body.password,
        client_ip=client_ip(request),
        device_token=_device_token(request),
    )
    return {"options": options}


@router.post("/bind/finish", summary="Привязать это устройство: ответ ключа")
async def bind_finish(request: Request, body: FinishBody) -> dict[str, str]:
    """Владельцу — пуш на все подписки; нажатие ведёт на главную отеля, где плашка."""
    staff = await _actor(request)
    info = await staff_passkey_binding.finish_binding(
        staff,
        body.credential,
        user_agent=request.headers.get("user-agent", ""),
        notice_url=f"/staff/{staff.tenant_slug}",
    )
    return {"device_label": info.device_label}


@router.post("/devices/{passkey_id}/revoke", summary="Отвязать своё устройство (с паролем)")
async def revoke_device(
    request: Request, passkey_id: uuid.UUID, body: PasswordBody
) -> dict[str, bool]:
    """Только свой ключ: чужой id отвечает `revoked: false`, как уже отвязанный."""
    staff = await _actor(request)
    revoked = await staff_passkeys.revoke_own_passkey(
        staff,
        passkey_id,
        body.password,
        client_ip=client_ip(request),
        device_token=_device_token(request),
    )
    return {"revoked": revoked}


@router.post("/sample", summary="Сохранить образец подписи")
async def save_sample(request: Request, body: memos_api.SignatureStrokes) -> dict[str, bool]:
    """Новый образец; прежний гасится и остаётся у листов, где он уже стоит (§4)."""
    staff = await _actor(request)
    await memos_api.set_signature_sample(staff, body)
    return {"ok": True}
