"""Плашка «Привязано устройство для подписи» на каждой странице отеля (spec 0077 §4, §9;
ADR-008 §8; #936, PR 4б-1).

Пуш о привязке уходит сразу (`staff_passkey_notice.announce_binding`); плашка — второй
путь: её видят другие сессии владельца, пока он не ответил. Строит её
`pages.hotel_page` через `notice_context`, рисует `layout.html`, кнопки —
`static/passkey_notice.js`. «Это я» закрывает плашку; «Это не я» — с паролем кабинета:
гасит ключ и все остальные входы учётки (В2-4). Раздела записок действия не требуют:
выключенный у отеля раздел не отменяет чужое устройство, которое уже привязано.
Канон JSON-действий — `queue_api.py`: CSRF-щит, `require_role` любого уровня.
"""

from __future__ import annotations

import uuid
from typing import Any, Final
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from hospitality.platform import staff_auth, staff_passkey_notice
from hospitality.platform.config import TenantConfig
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.clientip import client_ip
from hospitality.shared.logging import get_logger
from hospitality.staff_portal import browser
from hospitality.staff_portal.browser import require_json_same_origin

logger = get_logger(module=__name__)

router = APIRouter(prefix="/{tenant_slug}/api/passkey-notice", tags=["staff-portal"])

_any_role: Final = staff_auth.require_role(StaffRole.STAFF, StaffRole.SENIOR, StaffRole.MANAGER)

# Тексты сотруднику (§9; новые — запись founder-review PR #987).
DISOWN_HINT: Final = (
    "Введите пароль кабинета: устройство отвяжется, а все другие входы в вашу учётную запись "
    "закроются."
)
DISOWNED_TEXT: Final = "Устройство отвязано. Попросите у менеджера новый пароль."
# Плашку закрыли из другого входа раньше, чем владелец ответил (ревью PR #977, R2).
GONE_TEXT: Final = (
    "Это устройство уже подтвердили из другого входа. Если это были не вы — попросите "
    "у менеджера новый пароль и «Сбросить подпись»."
)
PASSWORD_TEXT: Final = "Пароль не подошёл."
ATTEMPTS_TEXT: Final = "Слишком много попыток. Подождите и попробуйте снова."
FAILED_TEXT: Final = "Не получилось. Попробуйте ещё раз."


class DisownBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=256)


async def notice_context(staff: StaffContext, config: TenantConfig | None) -> dict[str, Any] | None:
    """Плашки о непрочитанных привязках этой сессии; None — показывать нечего."""
    bindings = await staff_passkey_notice.unseen_bindings(staff)
    if not bindings:
        return None
    zone = config.tzinfo if config is not None else ZoneInfo("UTC")
    api = f"/staff/{staff.tenant_slug}/api/passkey-notice"
    return {
        "disown_hint": DISOWN_HINT,
        "disowned_text": DISOWNED_TEXT,
        "gone_text": GONE_TEXT,
        "password_text": PASSWORD_TEXT,
        "attempts_text": ATTEMPTS_TEXT,
        "failed_text": FAILED_TEXT,
        "items": [
            {
                "id": str(binding.id),
                "text": staff_passkey_notice.binding_notice_text(
                    binding.device_label,
                    staff_passkey_notice.format_bound_at(binding.created_at, zone),
                ),
                "ack_url": f"{api}/{binding.id}/ack",
                "disown_url": f"{api}/{binding.id}/disown",
            }
            for binding in bindings
        ],
    }


async def _actor(request: Request) -> StaffContext:
    require_json_same_origin(request)
    return await _any_role(request)


@router.post("/{passkey_id}/ack", summary="«Это я»: плашка о привязке закрыта")
async def acknowledge(request: Request, passkey_id: uuid.UUID) -> dict[str, bool]:
    """`ok: false` — закрывать нечего: уже закрыта, ключ свой из этой сессии или чужой."""
    staff = await _actor(request)
    acknowledged = await staff_passkey_notice.acknowledge_binding(staff, passkey_id)
    # «Это я» снимает предупреждение навсегда — след для разбора спора о подписи.
    logger.info(
        "staff.passkey_acknowledged",
        user_id=str(staff.user_id),
        passkey_id=str(passkey_id),
        acknowledged=acknowledged,
    )
    return {"ok": acknowledged}


@router.post("/{passkey_id}/disown", summary="«Это не я»: отвязать устройство с паролем")
async def disown(request: Request, passkey_id: uuid.UUID, body: DisownBody) -> dict[str, bool]:
    """Пароль — под бюджетами входа (ERR-AUTH-017, ERR-AUTH-006); `ok: false` — гасить
    нечего из этой сессии (плашку уже закрыли «Это я», ключ погашен или привязан ею)."""
    staff = await _actor(request)
    disowned = await staff_passkey_notice.disown_binding(
        staff,
        passkey_id,
        body.password,
        client_ip=client_ip(request),
        device_token=request.cookies.get(browser.STAFF_DEVICE_COOKIE),
    )
    return {"ok": disowned}
