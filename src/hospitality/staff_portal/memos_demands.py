"""Требование объяснения на экранах «Служебных записок» (spec 0077 §6, §9; #936, PR 5б).

Требование в системе — не документ, а способ вручить бумажное и зафиксировать получение
(§6.1): руководитель прикладывает фото подписанного листа, сотрудник жмёт «Получил(а)»
с подтверждением — от этого момента идёт срок. Здесь — строки требований во вкладках
реестра, страница требования обеим сторонам и форма «Затребовать объяснение». Данные
и право — только через `modules/memos/api.py`: требование видят затребовавший и
сотрудник (иначе ERR-MEMO-001, 404), «Получил(а)» — только сотрудник.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, tzinfo
from typing import Any

from hospitality.modules.memos import api as memos_api
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import StaffContext
from hospitality.platform.staff_confirm import ConfirmationMethod, ProofStatus
from hospitality.staff_portal import memos_search
from hospitality.staff_portal import memos_texts as texts
from hospitality.staff_portal.checkin import format_local
from hospitality.staff_portal.memos import (
    base_path,
    card_href,
    confirm_context,
    demand_href,
    tabs_context,
)
from hospitality.staff_portal.memos_people import demand_targets, directory, person_name

State = memos_api.DemandState


def _day(moment: datetime | None, zone: tzinfo) -> str:
    return format_local(moment, zone)[:10] if moment is not None else ""


def state_text(demand: memos_api.DemandSummary, zone: tzinfo) -> str:
    """Состояние требования строкой реестра (§6): «Получено, срок до …» и т. д."""
    return texts.DEMAND_STATE[demand.state].format(day=_day(demand.due_at, zone))


def list_rows(
    staff: StaffContext,
    demands: list[memos_api.DemandSummary],
    condition: memos_search.MemoFilter,
    name: Callable[[uuid.UUID], str],
    zone: tzinfo,
) -> list[tuple[datetime, dict[str, Any]]]:
    """Строки требований вкладки реестра с ключом сортировки — временем последнего шага."""
    return [
        (
            demand.received_at or demand.created_at,
            {
                "href": demand_href(staff, demand.id),
                "demand": True,
                "number": texts.DEMAND_TITLE,
                "title": "",
                "status": state_text(demand, zone),
                "status_key": demand.state.value,
                "author": name(demand.demanded_by_id),
                "addressee": name(demand.employee_id),
                "day": _day(demand.created_at, zone),
            },
        )
        for demand in demands
        if memos_search.matches_demand(demand, condition, name, zone)
    ]


def receipt_line(card: memos_api.DemandCard, zone: tzinfo) -> str | None:
    """«Получение: подтверждено …» — способ теми же словами, что у листа подписей (§9)."""
    if card.received_at is None or card.received_method is None:
        return None
    when = format_local(card.received_at, zone)
    if card.received_method is ConfirmationMethod.PASSWORD:
        method = texts.BY_PASSWORD.format(when=when)
    elif card.received_proof_status is ProofStatus.DISOWNED:
        method = texts.BY_DISOWNED.format(when=when)
    elif card.received_proof_status is ProofStatus.VERIFIED:
        method = texts.BY_DEVICE.format(when=when)
    else:
        method = texts.RECEIPT_MISMATCH.format(when=when)
    return texts.RECEIVED_LINE.format(method=method)


def _state_line(card: memos_api.DemandCard, employee: bool, boss: str, zone: tzinfo) -> str:
    due = _day(card.due_at, zone)
    if card.state is State.EXPLAINED:
        return texts.DEMAND_EXPLAINED
    if employee:
        if card.state is State.AWAITING_RECEIPT:
            return texts.DEMAND_TO_RECEIVE.format(boss=boss)
        if card.state is State.EXPIRED:
            return texts.DEMAND_EXPIRED_EMPLOYEE.format(day=due)
        return texts.DEMAND_OPEN.format(day=due)
    if card.state is State.AWAITING_RECEIPT:
        return texts.DEMAND_NOT_RECEIVED
    if card.state is State.EXPIRED:
        return texts.DEMAND_EXPIRED_BOSS.format(day=due)
    return texts.DEMAND_RECEIVED.format(day=due)


async def page_context(staff: StaffContext, demand_id: uuid.UUID, zone: tzinfo) -> dict[str, Any]:
    """Страница требования (шаблон `memo_demand.html`) затребовавшему и сотруднику."""
    card = await memos_api.get_demand(staff, demand_id)
    people = await directory(staff)
    employee = card.employee_id == staff.user_id
    boss = person_name(people, card.demanded_by_id)
    path = base_path(staff.tenant_slug)
    can_write = employee and card.state in (State.OPEN, State.EXPIRED)
    return {
        **tabs_context(staff, None),
        **confirm_context(staff),
        "demand_id": str(card.id),
        "title": texts.DEMAND_TITLE,
        "demanded_by": boss,
        "employee": person_name(people, card.employee_id),
        "created": _day(card.created_at, zone),
        "photo_href": f"{demand_href(staff, card.id)}/photo",
        "state_line": _state_line(card, employee, boss, zone),
        "state_key": card.state.value,
        "receipt": receipt_line(card, zone),
        "can_receive": employee and card.state is State.AWAITING_RECEIPT,
        "receive_button": texts.RECEIVE_BUTTON,
        "write_href": f"{path}/explanations/new?demand={card.id}" if can_write else None,
        "write_button": texts.WRITE_EXPLANATION,
        "explanations": [
            {
                "href": card_href(staff, row.id),
                "number": row.number or texts.STATUS[memos_api.MemoStatus.DRAFT],
                "sent": (
                    texts.EXPLANATION_SUBMITTED.format(when=format_local(row.sent_at, zone))
                    if row.sent_at is not None
                    else None
                ),
                "late": texts.EXPLANATION_LATE if row.late else None,
            }
            for row in card.explanations
        ],
    }


async def new_context(staff: StaffContext) -> dict[str, Any]:
    """Форма «Затребовать объяснение» (шаблон `memo_demand_new.html`): `senior` и
    `manager`; кого предложить — `memos_people.demand_targets`."""
    people = await directory(staff)
    return {
        **tabs_context(staff, None),
        "api": f"/staff/{staff.tenant_slug}/api/memos/demands",
        "title": texts.DEMAND_NEW,
        "intro": texts.DEMAND_INTRO,
        "photo_hint": texts.DEMAND_PHOTO_HINT,
        "senior_hint": texts.DEMAND_SENIOR_HINT if staff.role_key is StaffRole.SENIOR else None,
        "send_button": texts.DEMAND_SEND,
        "employees": demand_targets(staff, people),
        "messages": {
            **texts.DEMAND_MESSAGES,
            "members_changed": texts.MESSAGES["members_changed"],
            "failed": texts.MESSAGES["failed"],
        },
        "back_href": f"{base_path(staff.tenant_slug)}/mine",
    }
