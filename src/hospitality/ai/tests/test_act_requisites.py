"""Фото реквизитов для АВР — моделью через шлюз (spec 0049 Р-13, PR 5 серии #493).

Fake-адаптер шлюза вместо модели (ADR-007): что модель прочтёт на настоящем фото,
мерит офлайн-прогон `ai/evals/act_requisites_scan.py` с согласия основателя.
Здесь — что подписчик спрашивает, что делает с ответом и с отказом.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from hospitality.ai import act_requisites
from hospitality.ai.gateway.api import MockLlmProvider, ToolCall
from hospitality.ai.prompts import load_prompt
from hospitality.modules.documents import api as documents_api
from hospitality.modules.documents.api import FileTextState, UploadInput
from hospitality.modules.documents.tests.conftest import (
    Hotel,
    Hotels,
    hotels,  # noqa: F401  (фикстура: два отеля со службой «Бухгалтерия»)
    submit_form,
)
from hospitality.shared.config import get_settings
from hospitality.shared.db import utc_now
from hospitality.shared.events import deliver_pending_events, subscribe
from hospitality.shared.tenancy import tenant_context
from tests.conftest import fake_file_storage  # noqa: F401  (реимпорт фикстуры для pytest)
from tests.test_file_content import make_png

TOOL_NAME = act_requisites.TOOL_NAME
BIN = "940140000385"
IIK = "KZ86125KZT5004100100"


@pytest.fixture
def documents_model(hotels: Hotels) -> Iterator[None]:
    """Назначение `documents` включено, одна попытка провайдера — без пауз ретрая."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("LLM_MODEL_DOCUMENTS", "claude-sonnet-5")
        patch.setenv("LLM_MAX_ATTEMPTS", "1")
        get_settings.cache_clear()
        yield
    get_settings.cache_clear()


def _recorded(**arguments: Any) -> MockLlmProvider:
    return MockLlmProvider(
        text="", tool_calls=[ToolCall(id="call-1", name=TOOL_NAME, arguments=arguments)]
    )


async def _photo_request(hotel: Hotel, note: str = "") -> uuid.UUID:
    with tenant_context(hotel.tenant_id):
        read = await documents_api.submit_act_request(
            submit_form(hotel, note=note), [UploadInput(filename="card.png", data=make_png())]
        )
        await documents_api.extract_requisites(read.id)
    return read.id


async def _row(hotel: Hotel) -> documents_api.ActRequestRow:
    with tenant_context(hotel.tenant_id):
        (row,) = await documents_api.list_act_requests(
            created_from=utc_now().replace(year=2000), created_before=utc_now().replace(year=3000)
        )
    return row


async def test_model_off_reads_nothing_and_costs_nothing(hotels: Hotels) -> None:
    act_id = await _photo_request(hotels.a)
    provider = _recorded(bin_iin=BIN)
    with tenant_context(hotels.a.tenant_id):
        filled = await act_requisites.read_requisites_from_scans(act_id, provider=provider)

    assert filled == 0
    assert provider.calls == []
    assert (await _row(hotels.a)).files[0].text_state is FileTextState.IMAGE


@pytest.mark.usefixtures("documents_model")
async def test_photo_goes_to_the_documents_purpose_and_fills_the_draft(hotels: Hotels) -> None:
    act_id = await _photo_request(hotels.a)
    provider = _recorded(
        company_name="ТОО «Ромашка»",
        bin_iin=BIN,
        iik="KZ86 125K ZT50 0410 0100",
        bik="HSBKKZKX",
        bank_name="АО «Народный Банк Казахстана»",
        address="",
    )
    with tenant_context(hotels.a.tenant_id):
        filled = await act_requisites.read_requisites_from_scans(act_id, provider=provider)
    row = await _row(hotels.a)

    (request,) = provider.calls
    (message,) = request.messages
    (image,) = message.images
    assert request.purpose == "documents"
    assert request.forced_tool == TOOL_NAME
    assert [tool.name for tool in request.tools] == [TOOL_NAME]
    assert request.system == load_prompt(act_requisites.PROMPT_NAME)
    assert image.media_type == "image/webp"
    assert filled == 5
    assert row.requisites.bin_iin == BIN
    assert row.requisites.iik == IIK
    assert row.requisites.company_name == "ТОО «Ромашка»"
    assert row.files[0].text_state is FileTextState.VISION
    assert row.status is documents_api.RequisitesStatus.TO_CHECK


@pytest.mark.usefixtures("documents_model")
async def test_gateway_refusal_leaves_the_photo_for_the_human(hotels: Hotels) -> None:
    """Таймаут, бюджет, ошибка провайдера — черновика нет, событие не падает."""
    act_id = await _photo_request(hotels.a)
    provider = MockLlmProvider(timeouts_before_success=5)
    with tenant_context(hotels.a.tenant_id):
        filled = await act_requisites.read_requisites_from_scans(act_id, provider=provider)
    row = await _row(hotels.a)

    assert filled == 0
    assert len(provider.calls) == 1
    assert row.requisites.bin_iin == ""
    assert row.files[0].text_state is FileTextState.IMAGE


@pytest.mark.usefixtures("documents_model")
async def test_answer_without_the_tool_call_or_with_non_strings_writes_nothing(
    hotels: Hotels,
) -> None:
    act_id = await _photo_request(hotels.a)
    with tenant_context(hotels.a.tenant_id):
        text_only = await act_requisites.read_requisites_from_scans(
            act_id, provider=MockLlmProvider(text=f"БИН {BIN}")
        )
        not_strings = await act_requisites.read_requisites_from_scans(
            act_id, provider=_recorded(bin_iin=int(BIN), iik=[IIK])
        )
    row = await _row(hotels.a)

    assert (text_only, not_strings) == (0, 0)
    assert (row.requisites.bin_iin, row.requisites.iik) == ("", "")
    assert row.files[0].text_state is FileTextState.IMAGE


@pytest.mark.usefixtures("documents_model")
async def test_subscriber_parses_text_first_and_the_model_fills_the_rest(
    hotels: Hotels, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Одна доставка события: разбор без модели, затем фото — в пустые поля."""
    provider = _recorded(bin_iin="971240001315", bik="HSBKKZKX")
    monkeypatch.setattr("hospitality.ai.gateway.service.provider_for_model", lambda model: provider)
    subscribe(documents_api.ActRequestSubmitted, act_requisites.handle_act_request_submitted)
    with tenant_context(hotels.a.tenant_id):
        await documents_api.submit_act_request(
            submit_form(hotels.a, note=f"БИН {BIN}"),
            [UploadInput(filename="card.png", data=make_png())],
        )

    await deliver_pending_events()
    row = await _row(hotels.a)

    assert row.extraction_state is documents_api.ExtractionState.DONE
    assert row.requisites.bin_iin == BIN
    assert row.requisites.bik == "HSBKKZKX"
    assert row.files[0].text_state is FileTextState.VISION
