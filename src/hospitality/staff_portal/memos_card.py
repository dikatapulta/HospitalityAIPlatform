"""Контекст страниц записок: вкладки реестра, карточка с листом подписей и форма
черновика (spec 0077 §3, §5, §9; #936, PR 4б-2).

Данные — только через `modules/memos/api.py`: участника и статус проверяет модуль
(чужая запись — ERR-MEMO-001, 404). Здесь — что показать вошедшему: имена и службы
участников, строки листа подписей текстами §9 и кнопки, которые ему положены по роли
в записи. Право кнопки — подсказка: действие проверяет модуль ещё раз.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any, Final

from fastapi import HTTPException

from hospitality.modules.memos import api as memos_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform import staff_team
from hospitality.platform.config import TenantConfig
from hospitality.platform.models import MembershipStatus, StaffRole, UserStatus
from hospitality.platform.staff_auth import StaffContext
from hospitality.platform.staff_confirm import ConfirmationMethod, ProofStatus
from hospitality.staff_portal import memos_texts as texts
from hospitality.staff_portal.checkin import format_local
from hospitality.staff_portal.memos import BOXES, base_path, strokes_points, tabs_context
from hospitality.staff_portal.team import services_label

Status = memos_api.MemoStatus
Role = memos_api.SignatureRole

# Просмотров на карточке — последние столько; остальные — числом.
VIEWS_SHOWN: Final = 50
ADDRESSEE_ROLES: Final = (StaffRole.SENIOR, StaffRole.MANAGER)
_ROLE_ORDER: Final = {Role.AUTHOR: 0, Role.SIGNER: 1, Role.ADDRESSEE: 2}
# Ушедший из отеля участник остаётся в листе; имени нет — учётку стёрли (spec 0044 §5).
GONE_NAME: Final = "Бывший сотрудник"


@dataclass(frozen=True)
class Person:
    name: str
    services: str
    role: StaffRole
    active: bool


async def directory(staff: StaffContext) -> dict[uuid.UUID, Person]:
    """Сотрудники отеля по id: имя и службы — всем уровням (§8). Суперадмина нет."""
    members = await staff_team.list_tenant_members(staff.tenant_id)
    names = {category.key: category.name for category in await requests_api.list_categories()}
    return {
        member.user_id: Person(
            name=member.display_name,
            services=services_label(member.service_keys, names),
            role=member.role_key,
            active=member.membership_status is MembershipStatus.ACTIVE
            and member.user_status is UserStatus.ACTIVE,
        )
        for member in members
        if not member.is_platform_admin
    }


def _name(people: dict[uuid.UUID, Person], user_id: uuid.UUID) -> str:
    person = people.get(user_id)
    return person.name if person else GONE_NAME


def card_href(staff: StaffContext, memo_id: uuid.UUID) -> str:
    return f"{base_path(staff.tenant_slug)}/{memo_id}"


def confirm_context(staff: StaffContext) -> dict[str, Any]:
    """Окно подтверждения (`_memo_confirm.html`) и ответы действий для JS."""
    return {
        "memos_api": f"/staff/{staff.tenant_slug}/api/memos",
        "confirm_messages": texts.MESSAGES,
        "signature_href": f"{base_path(staff.tenant_slug)}/signature",
    }


def _day(moment: datetime, zone: tzinfo) -> str:
    return format_local(moment, zone)[:10]


async def list_context(staff: StaffContext, tab: str, zone: tzinfo) -> dict[str, Any]:
    """Вкладка реестра (шаблон `memo_list.html`): свежие сверху, без текста записи (§5)."""
    box = BOXES[tab]
    rows = await memos_api.list_memos(staff, box)
    people = await directory(staff) if rows else {}
    return {
        **tabs_context(staff, tab),
        "empty_text": texts.EMPTY[box],
        "new_href": f"{base_path(staff.tenant_slug)}/new",
        "memos": [
            {
                "href": card_href(staff, memo.id),
                "number": memo.number or texts.STATUS[Status.DRAFT],
                "title": memo.title,
                "status": texts.STATUS[memo.status],
                "status_key": memo.status.value,
                "author": _name(people, memo.author_id),
                "addressee": _name(people, memo.addressee_id),
                "day": _day(memo.sent_at or memo.created_at, zone),
            }
            for memo in rows
        ],
    }


def _response(card: memos_api.MemoCard, row: memos_api.SignatureRead) -> str:
    if row.role is Role.AUTHOR:
        return texts.SENT
    if row.role is Role.ADDRESSEE:
        return texts.STATUS[card.status]
    if row.response is memos_api.SignatureResponse.SIGNED:
        return texts.SIGNED
    if row.response is memos_api.SignatureResponse.REMARK:
        return texts.SIGNED_WITH_REMARK
    if card.status is Status.SIGNING:
        return texts.WAITING
    return texts.NO_ANSWER if card.submitted_at is not None else ""


def method_line(row: memos_api.SignatureRead, zone: tzinfo) -> str:
    """Способ подтверждения строки листа (§9, §4) — с особыми случаями ключа и пароля."""
    if row.signed_at is None or row.method is None:
        return ""
    when = format_local(row.signed_at, zone)
    if row.method is ConfirmationMethod.PASSWORD:
        line = texts.BY_PASSWORD.format(when=when)
        if row.password_reset_at is not None:
            line += "; " + texts.PASSWORD_WAS_RESET.format(day=_day(row.password_reset_at, zone))
        return line
    if row.proof_status is not ProofStatus.VERIFIED and row.proof_status is not (
        ProofStatus.DISOWNED
    ):
        return texts.BY_MISMATCH.format(when=when)
    if row.proof_status is ProofStatus.DISOWNED:
        line = texts.BY_DISOWNED.format(when=when)
    elif row.key_reset_revoked_at is not None:
        day = _day(row.key_reset_revoked_at, zone)
        line = texts.BY_RESET_REVOKED.format(day=day, when=when)
    else:
        line = texts.BY_DEVICE.format(when=when)
    if row.key_bound_after_reset_at is not None:
        line += "; " + texts.KEY_AFTER_RESET.format(day=_day(row.key_bound_after_reset_at, zone))
    return line


def _sheet(
    card: memos_api.MemoCard, people: dict[uuid.UUID, Person], zone: tzinfo
) -> list[dict[str, Any]]:
    rows = sorted(card.signatures, key=lambda row: _ROLE_ORDER[row.role])
    return [
        {
            "name": _name(people, row.user_id),
            "services": people[row.user_id].services if row.user_id in people else "",
            "role": texts.ROLE[row.role],
            "response": _response(card, row),
            "remark": row.remark,
            "method": method_line(row, zone),
            "sample": strokes_points(row.sample.strokes) if row.sample else None,
        }
        for row in rows
    ]


def _actions(staff: StaffContext, card: memos_api.MemoCard) -> dict[str, bool]:
    """Кнопки вошедшего по его роли в записи и статусу (§3)."""
    me = staff.user_id
    is_author = card.author_id == me
    my_signer_row = next(
        (row for row in card.signatures if row.user_id == me and row.role is Role.SIGNER), None
    )
    return {
        "edit": is_author and card.status is Status.DRAFT,
        "send": is_author and card.status is Status.DRAFT,
        "submit": is_author and card.status is Status.SIGNING,
        "withdraw": is_author and card.status in (Status.DRAFT, Status.SIGNING),
        "based_on": is_author and card.status is Status.RETURNED,
        "sign": card.status is Status.SIGNING
        and my_signer_row is not None
        and my_signer_row.response is None,
        "decide": card.addressee_id == me and card.status is Status.SUBMITTED,
    }


async def card_context(staff: StaffContext, memo_id: uuid.UUID, zone: tzinfo) -> dict[str, Any]:
    """Карточка записи участнику (§5): текст, лист подписей, решение, просмотры."""
    card = await memos_api.get_memo(staff, memo_id)
    people = await directory(staff)
    has_signers = any(row.role is Role.SIGNER for row in card.signatures)
    views = sorted(card.views, key=lambda view: view.viewed_at, reverse=True)
    path = base_path(staff.tenant_slug)
    return {
        **confirm_context(staff),
        "memo_id": str(card.id),
        "number": card.number,
        "title": card.title,
        "body": card.body,
        "status": texts.STATUS[card.status],
        "status_key": card.status.value,
        "author": _name(people, card.author_id),
        "addressee": _name(people, card.addressee_id),
        "sent": format_local(card.sent_at, zone) if card.sent_at else None,
        "based_on": (
            {"href": f"{path}/{card.based_on_id}", "number": card.based_on_number}
            if card.based_on_id is not None and card.based_on_number
            else None
        ),
        "content_changed": not card.content_intact,
        "content_changed_text": texts.CONTENT_CHANGED,
        "sheet": _sheet(card, people, zone),
        "decision": (
            {
                "status": texts.STATUS[card.status],
                "who": _name(people, card.addressee_id),
                "when": format_local(card.decided_at, zone),
                "comment": card.decision_comment,
            }
            if card.decided_at is not None
            else None
        ),
        "views": [
            {"name": _name(people, view.user_id), "when": format_local(view.viewed_at, zone)}
            for view in views[:VIEWS_SHOWN]
        ],
        "views_total": len(views),
        "actions": _actions(staff, card),
        "send_label": texts.SEND_TO_SIGNERS if has_signers else texts.SEND_TO_ADDRESSEE,
        "sign_request": texts.SIGN_REQUEST.format(author=_name(people, card.author_id)),
        "decision_hint": texts.DECISION_HINT,
        "submit_confirm": texts.SUBMIT_CONFIRM,
        "withdraw_confirm": texts.WITHDRAW_CONFIRM,
        "edit_href": f"{path}/{card.id}/edit",
        "remark_max": memos_api.REMARK_MAX_CHARS,
        "comment_max": memos_api.COMMENT_MAX_CHARS,
    }


def _choices(staff: StaffContext, people: dict[uuid.UUID, Person]) -> dict[str, Any]:
    """Кого можно выбрать: действующие сотрудники отеля, кроме вошедшего (§3.1)."""
    others = sorted(
        ((user_id, person) for user_id, person in people.items() if person.active),
        key=lambda item: item[1].name.casefold(),
    )
    others = [(user_id, person) for user_id, person in others if user_id != staff.user_id]
    return {
        "addressees": [
            {"id": str(user_id), "name": person.name, "services": person.services}
            for user_id, person in others
            if person.role in ADDRESSEE_ROLES
        ],
        "signers": [
            {"id": str(user_id), "name": person.name, "services": person.services}
            for user_id, person in others
        ],
    }


async def draft_context(
    staff: StaffContext, config: TenantConfig, memo_id: uuid.UUID | None
) -> dict[str, Any]:
    """Форма черновика: новая (`memo_id` None) или правка своего черновика."""
    people = await directory(staff)
    draft: dict[str, Any] = {
        "id": None,
        "template_key": None,
        "title": "",
        "body": "",
        "addressee_id": None,
        "signer_ids": [],
    }
    if memo_id is not None:
        card = await memos_api.get_memo(staff, memo_id)
        if card.author_id != staff.user_id or card.status is not Status.DRAFT:
            # Чужая или уже отправленная — правки нет, как неизвестный адрес.
            raise HTTPException(status_code=404)
        draft = {
            "id": str(card.id),
            "template_key": card.template_key,
            "title": card.title,
            "body": card.body,
            "addressee_id": str(card.addressee_id),
            "signer_ids": [str(row.user_id) for row in card.signatures if row.role is Role.SIGNER],
        }
    return {
        **confirm_context(staff),
        **_choices(staff, people),
        "draft": draft,
        "send_to_signers": texts.SEND_TO_SIGNERS,
        "send_to_addressee": texts.SEND_TO_ADDRESSEE,
        "templates": [
            {"key": template.key, "title": template.title, "body": template.body}
            for template in config.memo_templates
        ],
        "free_form": texts.FREE_FORM,
        "template_confirm": texts.TEMPLATE_CONFIRM,
        "draft_hint": texts.DRAFT_HINT,
        "max_signers": memos_api.MAX_SIGNERS,
        "title_max": memos_api.TITLE_MAX_CHARS,
        "body_max": memos_api.BODY_MAX_CHARS,
        "list_href": f"{base_path(staff.tenant_slug)}/mine",
    }
