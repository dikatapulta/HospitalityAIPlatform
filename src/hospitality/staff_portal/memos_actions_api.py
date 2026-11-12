"""Действия записок (spec 0077 §3, §4, §6; #936, PR 4б-2 и 5б): черновик, отзыв, отправка
руководителю, «на основе», черновик объяснительной и «Дополнить», действия под именем
через окно подтверждения. Требование объяснения — `memos_demands_api.py`.

Канон `memo_signature_api.py`: CSRF-щит `browser.require_json_same_origin`, личность —
`staff_auth.require_role` любого уровня, раздел выключен у отеля — 404. Кто вправе
действие и в каком статусе — модуль `memos` (ERR-MEMO-001 чужому, -002 не по статусу,
-003 без образца подписи). Действие под именем — два шага, как окно подтверждения:
`begin` (действие → вызов и параметры `navigator.credentials.get`, None — ключей нет)
и `complete` (ответ ключа или пароль кабинета → новый статус); отказ подтверждения —
ERR-AUTH-017, бюджет пароля — ERR-AUTH-006. Пуш следующему участнику уходит после
ответа (`BackgroundTasks`): сбой службы пуша шаг не отменяет. В логах — только id.
"""

from __future__ import annotations

import uuid
from typing import Literal, Self

from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from hospitality.modules.memos import api as memos_api
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.clientip import client_ip
from hospitality.staff_portal import browser, memos, memos_push
from hospitality.staff_portal import memos_texts as texts
from hospitality.staff_portal.browser import require_json_same_origin
from hospitality.staff_portal.memos import card_href

router = APIRouter(prefix="/{tenant_slug}/api/memos", tags=["staff-portal"])

# Пароль кабинета — тот же предел, что у формы входа и «Моей подписи».
_PASSWORD_MAX_LENGTH = 256
# Ответ браузера на `navigator.credentials.get` — сотни байт (как в `staff_confirm`).
_CREDENTIAL_MAX_LENGTH = 16_384


class IntentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: memos_api.MemoIntent


class CompleteBody(IntentBody):
    """Ответ окна подтверждения: ключ устройства (`credential`) или пароль — одно из двух."""

    challenge_id: str = Field(min_length=1, max_length=128)
    credential: str | None = Field(default=None, min_length=1, max_length=_CREDENTIAL_MAX_LENGTH)
    password: str | None = Field(default=None, min_length=1, max_length=_PASSWORD_MAX_LENGTH)

    @model_validator(mode="after")
    def _one_answer(self) -> Self:
        if (self.credential is None) == (self.password is None):
            raise ValueError("send either credential or password")
        return self


async def _actor(request: Request) -> StaffContext:
    """CSRF и личность — до всего; затем раздел у отеля: выключен — 404."""
    require_json_same_origin(request)
    staff = await memos.any_role(request)
    await memos.require_section(staff)
    return staff


def _answer(
    request: Request, body: CompleteBody
) -> memos_api.PasskeyAnswer | memos_api.PasswordAnswer:
    if body.credential is not None:
        return memos_api.PasskeyAnswer(
            challenge_id=body.challenge_id, credential_json=body.credential
        )
    assert body.password is not None  # валидатор: ровно одно из двух
    return memos_api.PasswordAnswer(
        challenge_id=body.challenge_id,
        password=body.password,
        client_ip=client_ip(request),
        device_token=request.cookies.get(browser.STAFF_DEVICE_COOKIE),
    )


async def _push_next(
    background: BackgroundTasks,
    staff: StaffContext,
    memo_id: uuid.UUID,
    step: Literal["sent", "submitted", "decided"],
) -> None:
    """Пуш тому, чья теперь очередь (§3): подписантам, руководителю или автору; поданная
    объяснительная — адресату своим текстом (§9)."""
    who = await memos_api.memo_participants(staff, memo_id)
    if step == "sent" and who.kind is memos_api.MemoKind.EXPLANATION:
        user_ids, body = [who.addressee_id], texts.PUSH_EXPLANATION
    elif step == "sent" and who.status is memos_api.MemoStatus.SIGNING:
        user_ids, body = who.signer_ids, texts.PUSH_SIGN
    elif step in ("sent", "submitted"):
        user_ids, body = [who.addressee_id], texts.PUSH_RECEIVED
    else:
        user_ids, body = [who.author_id], texts.PUSH_DECIDED
    background.add_task(
        memos_push.notify, staff.tenant_id, memo_id, user_ids, body, url=card_href(staff, memo_id)
    )


@router.post("/drafts", summary="Новый черновик записки")
async def create_draft(request: Request, body: memos_api.MemoDraftInput) -> dict[str, str]:
    staff = await _actor(request)
    memo_id = await memos_api.create_draft(staff, body)
    return {"id": str(memo_id), "href": card_href(staff, memo_id)}


@router.post("/{memo_id:uuid}/draft", summary="Сохранить свой черновик")
async def update_draft(
    request: Request, memo_id: uuid.UUID, body: memos_api.MemoDraftInput
) -> dict[str, str]:
    staff = await _actor(request)
    await memos_api.update_draft(staff, memo_id, body)
    return {"id": str(memo_id), "href": card_href(staff, memo_id)}


@router.post("/explanations", summary="Новая объяснительная: по требованию или адресату")
async def create_explanation(
    request: Request, body: memos_api.ExplanationDraftInput
) -> dict[str, str]:
    staff = await _actor(request)
    memo_id = await memos_api.create_explanation(staff, body)
    return {"id": str(memo_id), "href": card_href(staff, memo_id)}


@router.post("/{memo_id:uuid}/explanation", summary="Сохранить свой черновик объяснительной")
async def update_explanation(
    request: Request, memo_id: uuid.UUID, body: memos_api.ExplanationText
) -> dict[str, str]:
    staff = await _actor(request)
    await memos_api.update_explanation(staff, memo_id, body)
    return {"id": str(memo_id), "href": card_href(staff, memo_id)}


@router.post("/{memo_id:uuid}/supplement", summary="«Дополнить» свою поданную объяснительную")
async def supplement(
    request: Request, memo_id: uuid.UUID, body: memos_api.ExplanationText
) -> dict[str, str]:
    staff = await _actor(request)
    new_id = await memos_api.create_supplement(staff, memo_id, body)
    return {"id": str(new_id), "href": card_href(staff, new_id)}


@router.post("/{memo_id:uuid}/withdraw", summary="Отозвать свою записку")
async def withdraw(request: Request, memo_id: uuid.UUID) -> dict[str, bool]:
    staff = await _actor(request)
    await memos_api.withdraw(staff, memo_id)
    return {"ok": True}


@router.post("/{memo_id:uuid}/submit", summary="Отправить руководителю с подписями, что есть")
async def submit(
    request: Request, memo_id: uuid.UUID, background: BackgroundTasks
) -> dict[str, bool]:
    staff = await _actor(request)
    await memos_api.submit_to_addressee(staff, memo_id)
    await _push_next(background, staff, memo_id, "submitted")
    return {"ok": True}


@router.post("/{memo_id:uuid}/based-on", summary="Новая записка на основе возвращённой")
async def based_on(request: Request, memo_id: uuid.UUID) -> dict[str, str]:
    staff = await _actor(request)
    new_id = await memos_api.create_based_on(staff, memo_id)
    return {"id": str(new_id), "href": f"{card_href(staff, new_id)}/edit"}


@router.post("/{memo_id:uuid}/begin", summary="Окно подтверждения: начать действие")
async def begin(request: Request, memo_id: uuid.UUID, body: IntentBody) -> dict[str, str | None]:
    staff = await _actor(request)
    started = await memos_api.begin_action(staff, memo_id, body.intent)
    return {"challenge_id": started.challenge_id, "options": started.options_json}


@router.post("/{memo_id:uuid}/complete", summary="Окно подтверждения: ответ ключа или пароль")
async def complete(
    request: Request, memo_id: uuid.UUID, body: CompleteBody, background: BackgroundTasks
) -> dict[str, str]:
    staff = await _actor(request)
    status = await memos_api.complete_action(staff, memo_id, body.intent, _answer(request, body))
    if isinstance(body.intent, memos_api.SendIntent):
        await _push_next(background, staff, memo_id, "sent")
    elif isinstance(body.intent, memos_api.DecisionIntent):
        await _push_next(background, staff, memo_id, "decided")
    return {"status": status.value}
