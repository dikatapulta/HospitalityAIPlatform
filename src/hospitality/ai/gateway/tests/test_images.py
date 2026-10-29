"""Картинки во входе модели и назначение `documents` (spec 0049 Р-13, #530).

Главное: у запроса без картинок хэш промпта прежний (сверка по `prompt_hash` в
журнале не рвётся); байты картинки не попадают ни в хэш, ни в оценку резерва —
у неё своя верхняя граница; картинки уходят только адаптеру, который их умеет;
у фото реквизитов свой кошелёк и свой выключатель.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from collections.abc import Iterator
from contextlib import aclosing
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from hospitality.ai.gateway import service
from hospitality.ai.gateway.api import (
    ERR_AI_PURPOSE_NOT_CONFIGURED,
    LlmImage,
    LlmMessage,
    LlmRequest,
    MockLlmProvider,
    complete,
    stream,
    validate_configured_model,
)
from hospitality.ai.gateway.models import LlmCallLog
from hospitality.ai.gateway.openai_provider import OpenAIProvider
from hospitality.ai.gateway.price_list import MODEL_PRICING_USD_PER_MTOK
from hospitality.ai.gateway.provider import LlmProviderError
from hospitality.ai.gateway.tests.test_anthropic_provider import (
    _provider,
    _StubAsyncAnthropic,
    stub_sdk,  # noqa: F401  (фикстура: заглушка SDK Anthropic)
)
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

PHOTO = LlmImage(media_type="image/webp", data=b"RIFF\x00\x00\x00\x00WEBPVP8 fake")
TEXT_ONLY = LlmRequest(
    messages=[LlmMessage(role="user", content="Привет!")], system="Ты — консьерж отеля."
)
WITH_PHOTO = LlmRequest(
    messages=[LlmMessage(role="user", content="Прочитай реквизиты.", images=[PHOTO])],
    purpose="documents",
)


@pytest.fixture
def documents_model() -> Iterator[None]:
    """Назначение `documents` включено; свой `MonkeyPatch.context()` — канон test_purposes."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("LLM_MODEL", "claude-sonnet-5")
        patch.setenv("LLM_MODEL_DOCUMENTS", "claude-sonnet-5")
        patch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        get_settings.cache_clear()
        yield
    get_settings.cache_clear()


async def test_anthropic_sends_images_as_base64_blocks_before_the_text(
    stub_sdk: type[_StubAsyncAnthropic],  # noqa: F811
) -> None:
    await _provider().complete(WITH_PHOTO)

    stub = stub_sdk.last_instance
    assert stub is not None and stub.create_kwargs is not None
    assert stub.create_kwargs["messages"] == [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/webp",
                        "data": base64.standard_b64encode(PHOTO.data).decode("ascii"),
                    },
                },
                {"type": "text", "text": "Прочитай реквизиты."},
            ],
        }
    ]


def test_prompt_hash_without_images_is_unchanged() -> None:
    """Хэш — прежняя сериализация: поле `images` в ней не появляется вовсе."""
    before = TEXT_ONLY.model_dump_json(include={"system", "messages", "tools", "forced_tool"})

    assert "images" not in before
    assert service.compute_prompt_hash(TEXT_ONLY) == hashlib.sha256(before.encode()).hexdigest()


def test_image_enters_the_hash_by_digest_not_by_bytes() -> None:
    other = LlmImage(media_type="image/webp", data=PHOTO.data + b"!")
    changed = WITH_PHOTO.model_copy(
        update={"messages": [WITH_PHOTO.messages[0].model_copy(update={"images": [other]})]}
    )
    payload = service._request_payload(WITH_PHOTO)

    assert PHOTO.sha256 in payload
    assert base64.standard_b64encode(PHOTO.data).decode("ascii") not in payload
    assert service.compute_prompt_hash(WITH_PHOTO) != service.compute_prompt_hash(changed)


def test_reserve_adds_the_documented_ceiling_per_image(documents_model: None) -> None:
    """Картинка — не больше 4 784 токенов входа на любой модели (документация Anthropic)."""
    input_price, output_price = MODEL_PRICING_USD_PER_MTOK["claude-sonnet-5"]
    chars = len(service._request_payload(WITH_PHOTO))

    assert service._reserve_usd(WITH_PHOTO) == service._reserve_amount(
        chars=chars,
        input_price=input_price,
        output_tokens=WITH_PHOTO.max_tokens,
        output_price=output_price,
        images=1,
    )
    assert service._reserve_usd(WITH_PHOTO) > service._reserve_amount(
        chars=chars,
        input_price=input_price,
        output_tokens=WITH_PHOTO.max_tokens,
        output_price=output_price,
    )


async def test_openai_adapter_refuses_images_instead_of_dropping_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[object] = []

    async def create(**kwargs: object) -> None:
        sent.append(kwargs)

    monkeypatch.setattr(
        "hospitality.ai.gateway.openai_provider.openai.AsyncOpenAI",
        lambda **_: SimpleNamespace(responses=SimpleNamespace(create=create)),
    )
    provider = OpenAIProvider(api_key="test-key", model="gpt-6-luna", timeout_seconds=7.0)

    with pytest.raises(LlmProviderError, match="images"):
        await provider.complete(WITH_PHOTO)
    assert sent == []


async def test_images_are_refused_in_stream(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    with tenant_context(two_tenants[0]), pytest.raises(ValueError, match="картинки"):
        async with aclosing(stream(WITH_PHOTO, provider=MockLlmProvider())) as events:
            async for _ in events:
                pass


@pytest.mark.usefixtures("documents_model")
async def test_documents_call_is_logged_under_its_purpose(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a):
        await complete(WITH_PHOTO, provider=MockLlmProvider())
        async with session_scope() as session:
            (row,) = list(await session.scalars(select(LlmCallLog)))

    assert row.purpose == "documents"
    assert row.prompt_hash == service.compute_prompt_hash(WITH_PHOTO)


async def test_documents_purpose_is_off_by_default(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Пустой `LLM_MODEL_DOCUMENTS` — ERR-AI-006 до провайдера: фото никуда не уходит."""
    provider = MockLlmProvider()
    with tenant_context(two_tenants[0]), pytest.raises(AppError) as error:
        await complete(WITH_PHOTO, provider=provider)

    assert error.value.code == ERR_AI_PURPOSE_NOT_CONFIGURED
    assert provider.calls == []


@pytest.mark.parametrize(
    ("model", "anthropic_key", "expected"),
    [
        ("gpt-6-luna", "sk-ant-test", "не передаёт картинки"),
        ("claude-unknown", "sk-ant-test", "отсутствует в прайс-листе"),
        ("claude-sonnet-5", "", "ANTHROPIC_API_KEY не задан"),
    ],
)
def test_startup_rejects_unusable_documents_model(
    documents_model: None,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    anthropic_key: str,
    expected: str,
) -> None:
    monkeypatch.setenv("LLM_MODEL_DOCUMENTS", model)
    monkeypatch.setenv("ANTHROPIC_API_KEY", anthropic_key)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    get_settings.cache_clear()

    with pytest.raises(SystemExit) as error:
        validate_configured_model()
    assert "LLM_MODEL_DOCUMENTS" in str(error.value)
    assert expected in str(error.value)


def test_startup_passes_with_an_anthropic_documents_model(documents_model: None) -> None:
    validate_configured_model()  # не бросает — иначе тест упал бы здесь
