"""Шифртексты строк `act_requests` и `work_completion_acts` ↔ значения.

Все зашифрованные колонки — JSON через `platform.field_encryption` с AAD
`{tenant_id}/{таблица}/{id}/{колонка}` (spec 0049 Р-3, spec 0086 Р-5). Здесь —
единственное место, где модуль знает формат этих JSON.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from hospitality.modules.documents.models import ActRequest, WorkCompletionAct
from hospitality.modules.documents.schemas import RequisiteField, Requisites
from hospitality.platform.field_encryption import decrypt_field, encrypt_field, field_aad
from hospitality.shared.ports.accounting import ActDocument

TABLE = "act_requests"
GUEST_INPUT_COLUMN = "guest_input_ciphertext"
REQUISITES_COLUMN = "requisites_ciphertext"
ACTS_TABLE = "work_completion_acts"
ACT_CONTENT_COLUMN = "content_ciphertext"


@dataclass(frozen=True)
class GuestInput:
    bill_to: str = ""
    note: str = ""


@dataclass(frozen=True)
class StoredRequisites:
    values: Requisites
    ambiguous: frozenset[RequisiteField]


def encrypt_guest_input(row: ActRequest, guest_input: GuestInput) -> bytes | None:
    """Ничего не написано — None: пустой шифртекст хранить незачем."""
    if not guest_input.bill_to and not guest_input.note:
        return None
    payload = {"bill_to": guest_input.bill_to, "note": guest_input.note}
    return encrypt_field(json.dumps(payload, ensure_ascii=False), _aad(row, GUEST_INPUT_COLUMN))


def decrypt_guest_input(row: ActRequest) -> GuestInput:
    if row.guest_input_ciphertext is None:
        return GuestInput()
    payload = json.loads(decrypt_field(row.guest_input_ciphertext, _aad(row, GUEST_INPUT_COLUMN)))
    return GuestInput(bill_to=str(payload["bill_to"]), note=str(payload["note"]))


def encrypt_requisites(row: ActRequest, stored: StoredRequisites) -> bytes:
    payload = {
        "values": stored.values.model_dump(),
        "ambiguous": sorted(field.value for field in stored.ambiguous),
    }
    return encrypt_field(json.dumps(payload, ensure_ascii=False), _aad(row, REQUISITES_COLUMN))


def decrypt_requisites(row: ActRequest) -> StoredRequisites:
    if row.requisites_ciphertext is None:
        return StoredRequisites(Requisites(), frozenset())
    payload = json.loads(decrypt_field(row.requisites_ciphertext, _aad(row, REQUISITES_COLUMN)))
    return StoredRequisites(
        values=Requisites.model_validate(payload["values"]),
        ambiguous=frozenset(RequisiteField(name) for name in payload["ambiguous"]),
    )


class ActSnapshot(BaseModel):
    """Снимок черновика АВР (spec 0086 Р-2): файл и то, по каким реквизитам он собран.

    `requisites` — все шесть полей строки на момент акта (в форму Р-1 идут три),
    `checked_*` — кто и когда нажал «Проверено». Decimal в JSON — строкой: суммы
    возвращаются из шифртекста до тиына.
    """

    model_config = ConfigDict(frozen=True)

    document: ActDocument
    requisites: Requisites
    checked_at: datetime
    checked_by_display_name: str | None


def encrypt_act(act: WorkCompletionAct, snapshot: ActSnapshot) -> bytes:
    return encrypt_field(snapshot.model_dump_json(), _act_aad(act))


def decrypt_act(act: WorkCompletionAct) -> ActSnapshot | None:
    """Обезличенный акт — None: снимка больше нет."""
    if act.content_ciphertext is None:
        return None
    return ActSnapshot.model_validate_json(decrypt_field(act.content_ciphertext, _act_aad(act)))


def _aad(row: ActRequest, column: str) -> str:
    return field_aad(row.tenant_id, TABLE, row.id, column)


def _act_aad(act: WorkCompletionAct) -> str:
    return field_aad(act.tenant_id, ACTS_TABLE, act.id, ACT_CONTENT_COLUMN)
