"""Шифртексты строки `act_requests` ↔ значения (spec 0049 Р-3).

Обе зашифрованные колонки — JSON через `platform.field_encryption` с AAD
`{tenant_id}/act_requests/{id}/{колонка}`. Здесь — единственное место, где
модуль знает формат этих JSON.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from hospitality.modules.documents.models import ActRequest
from hospitality.modules.documents.schemas import RequisiteField, Requisites
from hospitality.platform.field_encryption import decrypt_field, encrypt_field, field_aad

TABLE = "act_requests"
GUEST_INPUT_COLUMN = "guest_input_ciphertext"
REQUISITES_COLUMN = "requisites_ciphertext"


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


def _aad(row: ActRequest, column: str) -> str:
    return field_aad(row.tenant_id, TABLE, row.id, column)
