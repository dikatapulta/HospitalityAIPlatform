"""Раздел «Служебные записки» кабинета (spec 0077 §4, §5, §9; #936, PR 4б серии).

Вкладки «Ждут меня», «Мои», «Все» (PR 4б-2) — реестр записок, их контекст, карточка и
форма черновика — `memos_card.py`, действия — `memos_actions_api.py`; объяснительные и
требования объяснения (PR 5б) — `memos_explanations.py`, `memos_demands.py`. «Моя подпись»
(PR 4б-1): привязка устройства (`platform/staff_passkey_binding`), отвязка своего
с паролем, образец подписи пальцем (`modules/memos`). Здесь — права, вкладки, контекст
«Моей подписи» и её тексты (§9 и запись founder-review PR #987); действия подписи —
`memo_signature_api.py`, плашка «Это я / Это не я» — `passkey_notice_api.py`.

Права (§8, docs/RBAC.md): «Моя подпись» — любой уровень, только своё (`user_id`
вошедшего в каждом вызове). Привязать устройство — только во входе с «Запомнить меня»
и не суперадмину (ADR-008 §8): иначе страница объясняет, почему, и подписывать можно
паролем. Раздел выключен у отеля (`TenantConfig.memos_enabled`, умолчание `false`) —
пункта меню нет, страницы и действия отвечают 404, как неизвестный адрес.
"""

from __future__ import annotations

import uuid
from datetime import tzinfo
from typing import Any, Final

from fastapi import HTTPException

from hospitality.modules.memos import api as memos_api
from hospitality.platform import staff_auth, staff_passkeys
from hospitality.platform.config import TenantConfig
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import StaffContext
from hospitality.staff_portal import memos_texts, queue
from hospitality.staff_portal.checkin import format_local

# Тексты сотруднику (§9; новые — запись founder-review PR #987).
NAV_TITLE: Final = "Служебные записки"
NAV_HINT: Final = "Записки и объяснительные: подписи коллег и решение руководителя"
TABS: Final = {
    "waiting": "Ждут меня",
    "mine": "Мои",
    "all": "Все",
    "signature": "Моя подпись",
}
# Вкладка реестра → ящик модуля (§5); «Моя подпись» — своя страница.
BOXES: Final = {
    "waiting": memos_api.MemoBox.WAITING,
    "mine": memos_api.MemoBox.MINE,
    "all": memos_api.MemoBox.ALL,
}
SIGNATURE_INTRO: Final = (
    "Настройте подпись один раз. Дальше каждую подпись вы подтверждаете лицом или "
    "отпечатком на этом устройстве."
)
BIND_TEXT: Final = (
    "Телефон попросит Face ID, отпечаток или код блокировки. Ваше лицо и отпечаток остаются "
    "в телефоне: кабинет получает только отметку „владелец подтвердил“."
)
BIND_WARNING: Final = "Только свой телефон — не компьютер стойки."
NO_KEY_TEXT: Final = (
    "Это устройство не умеет подтверждать лицом или отпечатком. Подписывать можно паролем кабинета."
)
NOT_REMEMBERED_TEXT: Final = (
    "Привязать можно только своё устройство. Войдите на нём с галочкой „Запомнить меня“ — "
    "или подписывайте паролем кабинета."
)
SUPERADMIN_TEXT: Final = (
    "Вход суперадмина не привязывает устройство — подписывайте паролем кабинета."
)
DEVICES_EMPTY: Final = "Устройств нет — подписывать можно паролем кабинета."
SAMPLE_TEXT: Final = (
    "Распишитесь пальцем. Этот образец будет стоять в записках рядом с вашим именем."
)
# Ответы действий — показывает static/memo_signature.js по коду и причине отказа.
MESSAGES: Final = {
    "bound": "Устройство привязано: «{label}».",
    "bind_failed": "Не получилось привязать устройство. Попробуйте ещё раз.",
    "password": "Пароль не подошёл.",
    "too_many_attempts": "Слишком много попыток. Подождите и попробуйте снова.",
    "limit": "Привязано уже 5 устройств — отвяжите одно, чтобы добавить новое.",
    "already_bound": "Это устройство уже привязано.",
    "not_remembered": NOT_REMEMBERED_TEXT,
    "platform_admin": SUPERADMIN_TEXT,
    "sample_saved": "Образец сохранён.",
    "sample_empty": "Распишитесь в рамке, потом сохраните.",
    "failed": "Не получилось. Попробуйте ещё раз.",
}

# Счётчик «Ждут меня» у пункта меню: больше — «99+», как у очереди заявок.
WAITING_CAP: Final = 99

any_role: Final = staff_auth.require_role(StaffRole.STAFF, StaffRole.SENIOR, StaffRole.MANAGER)


def section_on(config: TenantConfig | None) -> bool:
    """Раздел включён у отеля (флаг тенанта, §2); отель без конфига — нет."""
    return config is not None and config.memos_enabled


async def require_section(staff: StaffContext) -> TenantConfig:
    """Конфиг отеля с включённым разделом; выключен — 404, как неизвестный адрес."""
    config = await queue.tenant_config(staff)
    if config is None or not section_on(config):
        raise HTTPException(status_code=404)
    return config


def base_path(tenant_slug: str) -> str:
    return f"/staff/{tenant_slug}/memos"


def card_href(staff: StaffContext, memo_id: uuid.UUID) -> str:
    return f"{base_path(staff.tenant_slug)}/{memo_id}"


def demand_href(staff: StaffContext, demand_id: uuid.UUID) -> str:
    return f"{base_path(staff.tenant_slug)}/demands/{demand_id}"


def confirm_context(staff: StaffContext) -> dict[str, Any]:
    """Окно подтверждения (`_memo_confirm.html`) и ответы действий для JS."""
    return {
        "memos_api": f"/staff/{staff.tenant_slug}/api/memos",
        "confirm_messages": memos_texts.MESSAGES,
        "signature_href": f"{base_path(staff.tenant_slug)}/signature",
    }


def bind_refusal(staff: StaffContext) -> str | None:
    """Почему этой сессии нельзя привязать устройство; None — можно (ADR-008 §8).

    То же условие, что `staff_passkey_binding._require_remembered`: здесь — чтобы
    страница объяснила заранее, а не отказом после пароля."""
    if staff.is_platform_admin:
        return SUPERADMIN_TEXT
    if staff.session_locks_at is not None:
        return NOT_REMEMBERED_TEXT
    return None


def strokes_points(strokes: memos_api.SignatureStrokes) -> list[str]:
    """Штрихи образца — атрибутом `points` у `<polyline>`: целые числа, без разметки."""
    return [" ".join(f"{x},{y}" for x, y in stroke) for stroke in strokes.strokes]


def tabs_context(staff: StaffContext, tab: str | None) -> dict[str, Any]:
    """Полоса вкладок (`_memo_tabs.html`); `tab` None — страница записи, ни одна не текущая."""
    path = base_path(staff.tenant_slug)
    return {
        "tenant_name": staff.tenant_name,
        "tab": tab,
        "tabs": [
            {"key": key, "title": title, "href": f"{path}/{key}"} for key, title in TABS.items()
        ],
        "hint": NAV_HINT,
    }


async def signature_page(staff: StaffContext, config: TenantConfig) -> dict[str, Any]:
    """Контекст вкладки «Моя подпись» (шаблон `memo_signature.html`)."""
    context = tabs_context(staff, "signature")
    context["api"] = f"/staff/{staff.tenant_slug}/api/memos/signature"
    context.update(await _signature_context(staff, config.tzinfo))
    return context


async def waiting_count(staff: StaffContext, config: TenantConfig | None) -> int:
    """Число «Ждут меня» у пункта меню (§5): подписать, решить и получить требование;
    раздел выключен — 0."""
    if not section_on(config):
        return 0
    box = memos_api.MemoBox.WAITING
    return len(await memos_api.list_memos(staff, box)) + len(
        await memos_api.list_demands(staff, box)
    )


async def _signature_context(staff: StaffContext, zone: tzinfo) -> dict[str, Any]:
    """«Моя подпись»: привязанные устройства, образец и можно ли привязать это."""
    devices = await staff_passkeys.list_passkeys(staff.user_id)
    sample = await memos_api.current_signature_sample(staff)
    return {
        "intro": SIGNATURE_INTRO,
        "bind_text": BIND_TEXT,
        "bind_warning": BIND_WARNING,
        "bind_refusal": bind_refusal(staff),
        "no_key_text": NO_KEY_TEXT,
        "devices_empty": DEVICES_EMPTY,
        "sample_text": SAMPLE_TEXT,
        "messages": MESSAGES,
        "devices": [
            {
                "id": str(device.id),
                "label": device.device_label,
                "created": format_local(device.created_at, zone)[:10],
                "last_used": (
                    format_local(device.last_used_at, zone)[:10] if device.last_used_at else None
                ),
            }
            for device in devices
        ],
        "sample": strokes_points(sample.strokes) if sample is not None else None,
    }
