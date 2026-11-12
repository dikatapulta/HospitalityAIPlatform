"""«Оформить» служебной записки (spec 0077 §7, §12 PR 6): запрос и разбор ответа.

Модели уходят текст и шаблон — и ничего больше; назначение `memos`, промпт
`memo_format_v1`. Ответ — текст без обрамляющих пробелов; оборванный и после повтора,
пустой и длиннее поля записки автору не отдаются. Провайдер — Fake-адаптер (ADR-007).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from hospitality.ai import memo_format
from hospitality.ai.gateway.api import (
    ERR_AI_INCOMPLETE_ANSWER,
    ERR_AI_PROVIDER_ERROR,
    MockTurn,
    ScriptedLlmProvider,
)
from hospitality.ai.prompts import load_prompt
from hospitality.modules.memos import api as memos_api
from hospitality.platform.memo_config import DEFAULT_MEMO_TEMPLATES
from hospitality.shared.config import get_settings
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

MODEL = "claude-sonnet-5"
TEXT = "в 412 опять течет кран в ванной со вчерашнего вечера"
MALFUNCTION = next(t for t in DEFAULT_MEMO_TEMPLATES if t.key == "malfunction")


@pytest.fixture
def memos_model() -> Iterator[None]:
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("LLM_MODEL_MEMOS", MODEL)
        patch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-never-sent")
        get_settings.cache_clear()
        yield
    get_settings.cache_clear()


def test_request_carries_text_and_template_only() -> None:
    request = memo_format.build_request(TEXT, MALFUNCTION)

    assert request.system == load_prompt("memo_format_v1")
    assert (request.purpose, request.max_tokens, request.tools) == ("memos", 1500, [])
    [message] = request.messages
    assert message.role == "user"
    assert message.content == (
        f'<template title="О неисправности">\n{MALFUNCTION.body}\n</template>\n'
        f"<text>\n{TEXT}\n</text>"
    )


def test_free_form_request_has_no_template_block() -> None:
    [message] = memo_format.build_request(TEXT, None).messages
    assert message.content == f"<text>\n{TEXT}\n</text>"


@pytest.mark.usefixtures("memos_model")
async def test_answer_is_the_formatted_text(demo_tenant: uuid.UUID) -> None:
    provider = ScriptedLlmProvider([MockTurn(text="\n Где: номер 412.\nПрошу: устранить. \n")])
    with tenant_context(demo_tenant):
        text = await memo_format.format_memo(TEXT, MALFUNCTION, provider=provider)
    assert text == "Где: номер 412.\nПрошу: устранить."


@pytest.mark.parametrize(
    ("turns", "code"),
    [
        # Оборван и после повтора вдвое (`complete_whole`).
        ([MockTurn(text="Где: но", stop_reason="max_tokens")] * 2, ERR_AI_INCOMPLETE_ANSWER),
        ([MockTurn(text="  ")], ERR_AI_PROVIDER_ERROR),
        ([MockTurn(text="а" * (memos_api.BODY_MAX_CHARS + 1))], ERR_AI_INCOMPLETE_ANSWER),
    ],
    ids=["truncated", "empty", "longer-than-memo"],
)
@pytest.mark.usefixtures("memos_model")
async def test_unusable_answer_is_not_given_to_the_author(
    demo_tenant: uuid.UUID, turns: list[MockTurn], code: str
) -> None:
    provider = ScriptedLlmProvider(list(turns), model=MODEL)
    with tenant_context(demo_tenant), pytest.raises(AppError) as error:
        await memo_format.format_memo(TEXT, None, provider=provider)
    assert error.value.code == code
