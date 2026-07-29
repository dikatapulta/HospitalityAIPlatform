"""Сборка сообщения гостю о частичном выполнении (spec 0030).

Проверяет контракт вызова, а не качество текста (поведение модели не
симулируем — §8, правило 7): что уходит модели, на каком языке её просят
писать и что модуль не подменяет собой решение о деградации.
"""

from __future__ import annotations

import uuid

import pytest

from hospitality.ai.closing_message import (
    PARTIAL_COMPLETION_PROMPT_NAME,
    compose_partial_completion,
)
from hospitality.ai.gateway.api import MockLlmProvider
from hospitality.ai.prompts import load_prompt
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context


async def test_compose_sends_request_note_and_target_language(demo_tenant: uuid.UUID) -> None:
    """Модель получает суть заявки, пометку персонала и код языка в системной инструкции."""
    provider = MockLlmProvider(text="Your request was done only in part: the vacuum broke.")
    with tenant_context(demo_tenant):
        text = await compose_partial_completion(
            summary="clean room 305",
            resolution_note="пылесос сломался",
            language_code="en",
            provider=provider,
        )
    assert text == "Your request was done only in part: the vacuum broke."
    (call,) = provider.calls
    assert "clean room 305" in call.messages[0].content
    assert "пылесос сломался" in call.messages[0].content
    assert call.system is not None
    assert '"en"' in call.system
    assert "{language_code}" not in call.system  # плейсхолдер подставлен, а не оставлен


async def test_compose_returns_empty_on_blank_model_answer(demo_tenant: uuid.UUID) -> None:
    """Пустой ответ отдаётся как пустая строка: решение о деградации — за каналом."""
    provider = MockLlmProvider(text="   ")
    with tenant_context(demo_tenant):
        assert (
            await compose_partial_completion(
                summary="убрать 305",
                resolution_note="пылесос сломался",
                language_code="ru",
                provider=provider,
            )
            == ""
        )


async def test_compose_does_not_swallow_provider_error(demo_tenant: uuid.UUID) -> None:
    """Ошибка провайдера пробрасывается (зеркально ai/translation.py): деградацию
    к шаблону выбирает вызывающая сторона, а не этот модуль."""
    provider = MockLlmProvider(timeouts_before_success=99)
    with tenant_context(demo_tenant), pytest.raises(AppError):
        await compose_partial_completion(
            summary="убрать 305",
            resolution_note="пылесос сломался",
            language_code="ru",
            provider=provider,
        )


def test_prompt_forbids_invented_promises() -> None:
    """Границы промпта — часть контракта: обещание, сочинённое моделью, исполнять
    некому, пока остаток работы не стал чьей-то задачей (spec 0030, issue #58)."""
    prompt = load_prompt(PARTIAL_COMPLETION_PROMPT_NAME)
    assert "never promise" in prompt.lower()
    assert "{language_code}" in prompt
