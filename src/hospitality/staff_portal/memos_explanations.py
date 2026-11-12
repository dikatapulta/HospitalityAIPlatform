"""Объяснительные на экранах «Служебных записок» (spec 0077 §6, §9; #936, PR 5б).

Объяснительная — та же запись модуля `memos` видом `explanation`: карточка у неё общая
с запиской (`memos_card`), а здесь — то, чего у записки нет: ссылка на требование,
отметка «после срока», примечание адресату объяснительной без требования, и своя форма
черновика — по требованию (адресат — затребовавший), по своей инициативе (адресат на
выбор: `senior` или `manager`), правка и «Дополнить» поданной. Кто вправе — модуль
(`create_explanation`, `update_explanation`, `create_supplement`); здесь — подсказка формы.
"""

from __future__ import annotations

import uuid
from datetime import tzinfo
from typing import Any

from fastapi import HTTPException

from hospitality.modules.memos import api as memos_api
from hospitality.platform.staff_auth import StaffContext
from hospitality.staff_portal import memos_texts as texts
from hospitality.staff_portal.checkin import format_local
from hospitality.staff_portal.memos import base_path, card_href, confirm_context, demand_href
from hospitality.staff_portal.memos_people import addressees, directory, person_name

Kind = memos_api.MemoKind
Status = memos_api.MemoStatus


async def card_extras(
    staff: StaffContext, card: memos_api.MemoCard, zone: tzinfo
) -> dict[str, Any]:
    """Строки карточки объяснительной (§6): требование, «после срока», примечание
    адресату без требования. У записки — пусто."""
    extras: dict[str, Any] = {"demand_link": None, "late_text": None, "no_demand_note": None}
    if card.kind is not Kind.EXPLANATION:
        return extras
    if card.demand_id is None:
        if card.addressee_id == staff.user_id:
            extras["no_demand_note"] = texts.NO_DEMAND_NOTE
        return extras
    # Объяснительную видят автор и адресат — это и есть сотрудник и затребовавший.
    demand = await memos_api.get_demand(staff, card.demand_id)
    extras["demand_link"] = {
        "href": demand_href(staff, demand.id),
        "text": texts.BY_DEMAND.format(day=format_local(demand.created_at, zone)[:10]),
    }
    if demand.due_at is not None and card.sent_at is not None and card.sent_at > demand.due_at:
        extras["late_text"] = texts.LATE.format(day=format_local(demand.due_at, zone)[:10])
    return extras


def _form(staff: StaffContext, heading: str, back_href: str) -> dict[str, Any]:
    return {
        **confirm_context(staff),
        "heading": heading,
        "back_href": back_href,
        "final_hint": texts.EXPLANATION_FINAL,
        "send_label": texts.SEND_EXPLANATION,
        "title_max": memos_api.TITLE_MAX_CHARS,
        "body_max": memos_api.BODY_MAX_CHARS,
        "addressees": [],
        "addressee_text": None,
    }


def _draft(
    *,
    memo_id: uuid.UUID | None = None,
    title: str = "",
    body: str = "",
    addressee_id: uuid.UUID | None = None,
    demand_id: uuid.UUID | None = None,
    supplement_of: uuid.UUID | None = None,
) -> dict[str, str]:
    return {
        "id": str(memo_id) if memo_id else "",
        "title": title,
        "body": body,
        "addressee_id": str(addressee_id) if addressee_id else "",
        "demand_id": str(demand_id) if demand_id else "",
        "supplement_of": str(supplement_of) if supplement_of else "",
    }


async def new_context(staff: StaffContext, demand_id: uuid.UUID | None) -> dict[str, Any]:
    """Новая объяснительная: по требованию — только тому, у кого требуют, и после
    «Получил(а)» (иначе 404, как неизвестный адрес); без требования — адресат на выбор."""
    people = await directory(staff)
    if demand_id is None:
        context = _form(staff, texts.EXPLANATION_NEW, f"{base_path(staff.tenant_slug)}/mine")
        context["addressees"] = addressees(staff, people)
        context["draft"] = _draft()
        return context
    demand = await memos_api.get_demand(staff, demand_id)
    if demand.employee_id != staff.user_id or demand.received_at is None:
        raise HTTPException(status_code=404)
    context = _form(staff, texts.EXPLANATION_NEW, demand_href(staff, demand.id))
    boss = person_name(people, demand.demanded_by_id)
    context["addressee_text"] = texts.EXPLANATION_TO.format(name=boss)
    context["draft"] = _draft(demand_id=demand.id)
    return context


async def edit_context(staff: StaffContext, card: memos_api.MemoCard) -> dict[str, Any]:
    """Правка своего черновика объяснительной (`memos_card.own_draft`). Адресата
    меняет только черновик без требования и не дополнение (модуль, `addressee_fixed`)."""
    if card.kind is not Kind.EXPLANATION:
        raise HTTPException(status_code=404)
    people = await directory(staff)
    context = _form(staff, texts.EXPLANATION_DRAFT, card_href(staff, card.id))
    if card.demand_id is None and card.based_on_id is None:
        context["addressees"] = addressees(staff, people)
    elif card.demand_id is not None:
        boss = person_name(people, card.addressee_id)
        context["addressee_text"] = texts.EXPLANATION_TO.format(name=boss)
    else:
        name = person_name(people, card.addressee_id)
        context["addressee_text"] = texts.EXPLANATION_ADDRESSEE.format(name=name)
    if card.based_on_number:
        context["heading"] = texts.SUPPLEMENT_TITLE.format(number=card.based_on_number)
    context["draft"] = _draft(
        memo_id=card.id, title=card.title, body=card.body, addressee_id=card.addressee_id
    )
    return context


async def supplement_context(staff: StaffContext, source_id: uuid.UUID) -> dict[str, Any]:
    """«Дополнить» свою поданную объяснительную (§6.4): новая запись со ссылкой на неё,
    тем же адресатом и требованием. Не своя, не поданная или записка — 404."""
    source = await memos_api.get_memo(staff, source_id)
    if (
        source.kind is not Kind.EXPLANATION
        or source.author_id != staff.user_id
        or source.status is not Status.SUBMITTED
        or source.number is None
    ):
        raise HTTPException(status_code=404)
    people = await directory(staff)
    heading = texts.SUPPLEMENT_TITLE.format(number=source.number)
    context = _form(staff, heading, card_href(staff, source.id))
    name = person_name(people, source.addressee_id)
    fixed = texts.EXPLANATION_TO if source.demand_id is not None else texts.EXPLANATION_ADDRESSEE
    context["addressee_text"] = fixed.format(name=name)
    context["draft"] = _draft(title=heading, supplement_of=source.id)
    return context
