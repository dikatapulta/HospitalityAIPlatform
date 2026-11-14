"""Контекст страниц записок: вкладки реестра с поиском (`memos_search`), карточка
с листом подписей и форма черновика (spec 0077 §3, §5, §9; #936, PR 4б-2 и 4б-3).
Лист подписей и решение отсюда берёт и печать (`memos_print`). Объяснительная (PR 5б) —
та же карточка со своими статусом, кнопками и строками (`memos_explanations`); требования
объяснения стоят в тех же вкладках (`memos_demands`).

Данные — только через `modules/memos/api.py`: участника и статус проверяет модуль
(чужая запись — ERR-MEMO-001, 404). Здесь — что показать вошедшему: имена и службы
участников, строки листа подписей текстами §9 и кнопки, которые ему положены по роли
в записи. Право кнопки — подсказка: действие проверяет модуль ещё раз.
"""

from __future__ import annotations

import uuid
from datetime import datetime, tzinfo
from typing import Any, Final

from fastapi import HTTPException

from hospitality.modules.memos import api as memos_api
from hospitality.platform.config import TenantConfig
from hospitality.platform.staff_auth import StaffContext
from hospitality.platform.staff_confirm import ConfirmationMethod, ProofStatus
from hospitality.staff_portal import memos_demands, memos_explanations, memos_search
from hospitality.staff_portal import memos_texts as texts
from hospitality.staff_portal.checkin import format_local
from hospitality.staff_portal.memos import (
    BOXES,
    base_path,
    card_href,
    confirm_context,
    format_on,
    strokes_points,
    tabs_context,
)
from hospitality.staff_portal.memos_people import (
    Person,
    addressees,
    can_demand,
    directory,
    option,
    others,
    person_name,
)

Status = memos_api.MemoStatus
Role = memos_api.SignatureRole
Kind = memos_api.MemoKind

# Просмотров на карточке — последние столько; остальные — числом.
VIEWS_SHOWN: Final = 50
_ROLE_ORDER: Final = {Role.AUTHOR: 0, Role.SIGNER: 1, Role.ADDRESSEE: 2}


def _day(moment: datetime, zone: tzinfo) -> str:
    return format_local(moment, zone)[:10]


async def list_context(
    staff: StaffContext, tab: str, zone: tzinfo, condition: memos_search.MemoFilter
) -> dict[str, Any]:
    """Вкладка реестра (шаблон `memo_list.html`): свежие сверху, без текста записи, с
    поиском и фильтрами по своим строкам (§5, `memos_search`). Требования объяснения
    вкладки (§5, §6) — строками рядом с записками, по времени последнего шага."""
    box = BOXES[tab]
    rows = await memos_api.list_memos(staff, box)
    demands = await memos_api.list_demands(staff, box)
    people = await directory(staff) if rows or demands else {}

    def name(user_id: uuid.UUID) -> str:
        return person_name(people, user_id)

    entries = [
        (
            memo.updated_at,
            {
                "href": card_href(staff, memo.id),
                "number": memo.number or texts.STATUS[Status.DRAFT],
                "title": memo.title,
                "status": texts.status_text(memo.kind, memo.status),
                "status_key": memo.status.value,
                "author": name(memo.author_id),
                "addressee": name(memo.addressee_id),
                "day": _day(memo.sent_at or memo.created_at, zone),
            },
        )
        for memo in rows
        if memos_search.matches(memo, condition, name, zone)
    ]
    entries += memos_demands.list_rows(staff, demands, condition, name, zone)
    entries.sort(key=lambda entry: entry[0], reverse=True)
    path = base_path(staff.tenant_slug)
    return {
        **tabs_context(staff, tab),
        **memos_search.form_context(condition, f"{path}/{tab}"),
        "empty_text": texts.SEARCH_EMPTY if condition.active else texts.EMPTY[box],
        "found_text": (
            texts.SEARCH_FOUND.format(count=len(entries)) if condition.active and entries else None
        ),
        "new_href": f"{path}/new",
        "explanation_href": f"{path}/explanations/new",
        "explanation_button": texts.EXPLANATION_NEW,
        "demand_new_href": f"{path}/demands/new" if can_demand(staff) else None,
        "demand_button": texts.DEMAND_NEW,
        "memos": [row for _, row in entries],
    }


def _response(card: memos_api.MemoCard, row: memos_api.SignatureRead) -> str:
    if row.role is Role.AUTHOR:
        return texts.EXPLANATION_SENT if card.kind is Kind.EXPLANATION else texts.SENT
    if row.role is Role.ADDRESSEE:
        return texts.status_text(card.kind, card.status)
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


def sheet_rows(
    card: memos_api.MemoCard, people: dict[uuid.UUID, Person], zone: tzinfo
) -> list[dict[str, Any]]:
    """Лист подписей (`_memo_sheet.html`): автор, подписанты, руководитель — текстами §9."""
    rows = sorted(card.signatures, key=lambda row: _ROLE_ORDER[row.role])
    return [
        {
            "name": person_name(people, row.user_id),
            "services": people[row.user_id].services if row.user_id in people else "",
            "role": texts.ROLE[row.role],
            "response": _response(card, row),
            "remark": row.remark,
            "method": method_line(row, zone),
            "sample": strokes_points(row.sample.strokes) if row.sample else None,
        }
        for row in rows
    ]


def decision_block(
    card: memos_api.MemoCard, people: dict[uuid.UUID, Person], zone: tzinfo
) -> dict[str, Any] | None:
    """Решение руководителя (§3.5); None — решения ещё нет."""
    if card.decided_at is None:
        return None
    return {
        "status": texts.status_text(card.kind, card.status),
        "who": person_name(people, card.addressee_id),
        "when": format_local(card.decided_at, zone),
        "comment": card.decision_comment,
    }


def _actions(staff: StaffContext, card: memos_api.MemoCard) -> dict[str, bool]:
    """Кнопки вошедшего по его роли в записи и статусу (§3); у объяснительной решения
    нет, а поданную автор дополняет (§6)."""
    me = staff.user_id
    is_author = card.author_id == me
    explanation = card.kind is Kind.EXPLANATION
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
        "decide": not explanation and card.addressee_id == me and card.status is Status.SUBMITTED,
        "supplement": explanation and is_author and card.status is Status.SUBMITTED,
    }


async def card_context(staff: StaffContext, memo_id: uuid.UUID, zone: tzinfo) -> dict[str, Any]:
    """Карточка записи участнику (§5): текст, лист подписей, решение, просмотры."""
    card = await memos_api.get_memo(staff, memo_id)
    people = await directory(staff)
    has_signers = any(row.role is Role.SIGNER for row in card.signatures)
    views = sorted(card.views, key=lambda view: view.viewed_at, reverse=True)
    path = base_path(staff.tenant_slug)
    if card.kind is Kind.EXPLANATION:
        send_label = texts.SEND_EXPLANATION
    else:
        send_label = texts.SEND_TO_SIGNERS if has_signers else texts.SEND_TO_ADDRESSEE
    return {
        **confirm_context(staff),
        **await memos_explanations.card_extras(staff, card, zone),
        "memo_id": str(card.id),
        "kind": texts.KIND[card.kind],
        "number": card.number,
        "title": card.title,
        "body": card.body,
        "status": texts.status_text(card.kind, card.status),
        "status_key": card.status.value,
        "author": person_name(people, card.author_id),
        "addressee": person_name(people, card.addressee_id),
        "sent": format_local(card.sent_at, zone) if card.sent_at else None,
        "based_on": (
            {
                "href": f"{path}/{card.based_on_id}",
                "label": texts.BASED_ON[card.kind],
                "number": card.based_on_number,
            }
            if card.based_on_id is not None and card.based_on_number
            else None
        ),
        "content_changed": not card.content_intact,
        "content_changed_text": texts.CONTENT_CHANGED,
        "sheet": sheet_rows(card, people, zone),
        "decision": decision_block(card, people, zone),
        "views": [
            {"name": person_name(people, view.user_id), "when": format_local(view.viewed_at, zone)}
            for view in views[:VIEWS_SHOWN]
        ],
        "views_total": len(views),
        "actions": _actions(staff, card),
        "send_label": send_label,
        "sign_request": texts.SIGN_REQUEST.format(author=person_name(people, card.author_id)),
        "decision_hint": texts.DECISION_HINT,
        "submit_confirm": texts.SUBMIT_CONFIRM,
        "withdraw_confirm": texts.WITHDRAW_CONFIRM,
        "edit_href": f"{path}/{card.id}/edit",
        "supplement_href": f"{path}/{card.id}/supplement",
        "supplement_button": texts.SUPPLEMENT,
        # Печать — только отправленной: черновик без номера и подписей (memos_print).
        "print_href": f"{path}/{card.id}/print" if card.sent_at is not None else None,
        "print_button": texts.PRINT_BUTTON,
        "remark_max": memos_api.REMARK_MAX_CHARS,
        "comment_max": memos_api.COMMENT_MAX_CHARS,
    }


def _choices(staff: StaffContext, people: dict[uuid.UUID, Person]) -> dict[str, Any]:
    """Кого можно выбрать: действующие сотрудники отеля, кроме вошедшего (§3.1)."""
    return {
        "addressees": addressees(staff, people),
        "signers": [option(user_id, person) for user_id, person in others(staff, people)],
    }


async def own_draft(staff: StaffContext, memo_id: uuid.UUID) -> memos_api.MemoCard:
    """Свой черновик для формы правки; чужая или уже отправленная — 404, как неизвестный
    адрес. Вид решает форма: записка — `draft_context`, объяснительная —
    `memos_explanations.draft_context`."""
    card = await memos_api.get_memo(staff, memo_id)
    if card.author_id != staff.user_id or card.status is not Status.DRAFT:
        raise HTTPException(status_code=404)
    return card


async def draft_context(
    staff: StaffContext, config: TenantConfig, card: memos_api.MemoCard | None
) -> dict[str, Any]:
    """Форма черновика записки: новая (`card` None) или правка своего (`own_draft`)."""
    people = await directory(staff)
    draft: dict[str, Any] = {
        "id": None,
        "template_key": None,
        "title": "",
        "body": "",
        "addressee_id": None,
        "signer_ids": [],
    }
    if card is not None:
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
        "format_on": format_on(staff),
        "format_button": texts.FORMAT_BUTTON,
        "format_busy": texts.FORMAT_BUSY,
        "format_hint": texts.FORMAT_HINT,
        "format_result": texts.FORMAT_RESULT,
        "format_accept": texts.FORMAT_ACCEPT,
        "format_undo": texts.FORMAT_UNDO,
        "format_messages": texts.FORMAT_MESSAGES,
        "draft_hint": texts.DRAFT_HINT,
        "max_signers": memos_api.MAX_SIGNERS,
        "title_max": memos_api.TITLE_MAX_CHARS,
        "body_max": memos_api.BODY_MAX_CHARS,
        "list_href": f"{base_path(staff.tenant_slug)}/mine",
    }
