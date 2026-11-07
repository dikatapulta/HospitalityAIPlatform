"""Новая просьба поверх невзятой карточки эскалации не теряется (spec 0039 §2.5, #863).

Находка прогона 05.11: «позовите сотрудника», затем «сменить номер» и «поздний
выезд» — бот дважды сказал «передал», а в кабинете была одна карточка без слова о
них. Теперь дедупликация дописывает каждую новую просьбу строкой в подробности
невзятой карточки. Сценарии и помощники — из `test_escalation_requests.py`.

F811 отключён на файл: фикстура-параметр `hotel` обязана называться как
импортированная фикстура — так pytest связывает их.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from hospitality.ai.escalation import EscalationReason
from hospitality.channels.common import escalation_request
from hospitality.channels.common.tests.test_escalation_requests import (  # noqa: F401  (фикстура `hotel`)
    _escalation_rows,
    _guest_says,
    _llm_down,
    _needs_human,
    _requests,
    _set_status,
    hotel,
)
from hospitality.modules.requests import api as requests_api

# 05:52 UTC — 10:52 в Алматы (UTC+5): строка пишет время по часам отеля (§9).
_NOW = datetime(2026, 11, 5, 5, 52, tzinfo=UTC)


def _freeze_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Вызовом в тесте, а не autouse-фикстурой: autouse поднял бы `monkeypatch`
    раньше временной БД, и его откат пришёлся бы после её удаления — DROP
    подключился бы к самой удаляемой базе."""
    monkeypatch.setattr(escalation_request, "utc_now", lambda: _NOW)


async def test_second_request_is_written_into_the_unclaimed_card(
    hotel: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сценарий находки: карточка одна, но в ней обе просьбы гостя."""
    tenant_id, conversation_id = hotel
    _freeze_clock(monkeypatch)
    _needs_human(
        monkeypatch,
        EscalationReason.GUEST_REQUESTED_STAFF,
        action_summary="гость просит, чтобы подошёл сотрудник",
    )
    await _guest_says(tenant_id, conversation_id, "позовите сотрудника")
    _needs_human(
        monkeypatch,
        EscalationReason.GUEST_REQUESTED_STAFF,
        action_summary="гость хочет сменить номер — шумно от лифта",
    )
    await _guest_says(tenant_id, conversation_id, "и ещё хочу сменить номер, тут шумно от лифта")

    (card,) = await _requests(tenant_id)
    assert card.details == (
        "Что бот понял: гость просит, чтобы подошёл сотрудник\n"
        "Ещё в 10:52: «и ещё хочу сменить номер, тут шумно от лифта». "
        "Что бот понял: гость хочет сменить номер — шумно от лифта"
    )
    first, second = await _escalation_rows(tenant_id)
    assert (first.request_id, second.request_id) == (card.id, None)


async def test_without_the_model_the_line_carries_the_guest_words(
    hotel: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Модель лежит — понимания бота нет, в строке только слова гостя."""
    tenant_id, conversation_id = hotel
    _freeze_clock(monkeypatch)
    _llm_down(monkeypatch)
    await _guest_says(tenant_id, conversation_id, "алло")
    await _guest_says(tenant_id, conversation_id, "ау, есть кто?")

    (card,) = await _requests(tenant_id)
    assert card.details == "Ещё в 10:52: «ау, есть кто?»"


async def test_ordinary_request_over_an_unclaimed_urgent_card_is_written_into_it(
    hotel: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, conversation_id = hotel
    _freeze_clock(monkeypatch)
    await _guest_says(tenant_id, conversation_id, "пожар")
    _llm_down(monkeypatch)
    await _guest_says(tenant_id, conversation_id, "алло")

    (card,) = await _requests(tenant_id)
    assert card.is_urgent
    assert card.details == "Ещё в 10:52: «алло»"


async def test_full_card_gives_way_to_a_new_one_and_the_next_line_goes_there(
    hotel: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Подробности первой полны — вторая просьба получает свою карточку, третья
    дописывается в неё, а не заводит ещё одну."""
    tenant_id, conversation_id = hotel
    _freeze_clock(monkeypatch)
    _needs_human(
        monkeypatch,
        EscalationReason.GUEST_REQUESTED_STAFF,
        action_summary="x" * escalation_request._DETAILS_MAX_CHARS,
    )
    await _guest_says(tenant_id, conversation_id, "позовите сотрудника")
    _llm_down(monkeypatch)
    await _guest_says(tenant_id, conversation_id, "алло")
    await _guest_says(tenant_id, conversation_id, "ау, есть кто?")

    first, second = await _requests(tenant_id)
    assert first.details is not None
    assert "алло" not in first.details
    assert second.summary == "Бот не смог ответить, гость ждёт сотрудника: «алло»"
    assert second.details == "Ещё в 10:52: «ау, есть кто?»"


async def test_claimed_card_takes_no_line_and_the_request_gets_its_own(
    hotel: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Взятую карточку сотрудник уже мог унести к гостю — новая просьба туда не
    дописывается, а заводит свою (как до #863)."""
    tenant_id, conversation_id = hotel
    _freeze_clock(monkeypatch)
    _llm_down(monkeypatch)
    await _guest_says(tenant_id, conversation_id, "алло")
    (first,) = await _requests(tenant_id)
    await _set_status(tenant_id, first.id, requests_api.RequestStatus.IN_PROGRESS)
    await _guest_says(tenant_id, conversation_id, "ау, есть кто?")

    claimed, fresh = await _requests(tenant_id)
    assert claimed.details is None
    assert fresh.summary == "Бот не смог ответить, гость ждёт сотрудника: «ау, есть кто?»"
