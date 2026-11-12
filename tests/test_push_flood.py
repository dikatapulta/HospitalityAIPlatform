"""Заливка заявок вручную и пуш на телефон (#971, находка О2-1 аудита 10.11).

Пуш строится тем же сигналом (`push_sweep` → `signal.build_signal`): после 60 заявок-
мусора срочная и эскалация остаются в журнале «уже прозвучало» ресепшена и менеджера —
выпади они из списка, журнал их бы забыл, а вернувшись, они прозвучали бы заново.
Сам сигнал под заливкой — `staff_portal/tests/test_entry_flood.py`.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid

from hospitality.integrations.web_push import FakeWebPushAdapter
from hospitality.platform.models import StaffRole
from hospitality.staff_portal.tests.test_entry_flood import _escalation, _flood, _hotel_with_staff
from hospitality.staff_portal.tests.test_queue import _make_request
from tests.test_push_sweep import (  # noqa: F401  (фикстуры для pytest)
    _journal,
    _member,
    _phone,
    _sweep,
    fake_push,
    hotel,
)


async def test_flood_keeps_urgent_and_escalation_in_push_list(
    hotel: uuid.UUID, fake_push: FakeWebPushAdapter
) -> None:
    users = await _hotel_with_staff(hotel)
    manager = await _member(hotel, (), StaffRole.MANAGER)
    await _phone(hotel, users["reception"], "reception")
    await _phone(hotel, manager, "manager")
    await _sweep()  # первое включение
    urgent = await _make_request(hotel, "течёт вода", room_number="305", is_urgent=True)
    escalation = await _escalation(hotel)
    await _sweep()

    await _flood(hotel)
    await _sweep()

    journal = await _journal(hotel)
    for user_id in (users["reception"], manager):
        assert f"new:{urgent.id}:{urgent.category_id}" in journal[user_id]
        assert f"new:{escalation.id}:{escalation.category_id}" in journal[user_id]
