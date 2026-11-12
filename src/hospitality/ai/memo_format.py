"""«Оформить» в черновике служебной записки (spec 0077 §7, PR 6 серии; #936).

Один вызов `complete_whole(purpose="memos")` с промптом `memo_format_v1`: текст автора
и, если выбран, шаблон отеля → тот же текст деловым языком, без добавленных фактов, сумм
и имён. Ответ — лишь предложение автору: «Принять» или «Вернуть мой текст» решает он,
отправляет — только с подтверждением (P-9, NG-4). Модели уходят текст и шаблон; имя,
логин, адресат и подписанты — нет (§7, PII_REGISTRY). Запрос один на кабинет и на
evals (`ai/evals/memo_format.py`).

Ошибки шлюза уходят вызывающему: ERR-AI-002 (дневной бюджет `memos`), ERR-AI-006 (модель
не задана), ERR-AI-001/003 (провайдер), ERR-AI-007 (ответ оборван и после повтора, или
модель отказалась). Пустой ответ и ответ длиннее поля записки — тоже не текст автору:
ERR-AI-003 и ERR-AI-007; текст сотруднику по коду выбирает кабинет.
"""

from __future__ import annotations

from typing import Final

from hospitality.ai.gateway import api as gateway
from hospitality.ai.prompts import load_prompt
from hospitality.modules.memos import api as memos_api
from hospitality.platform.memo_config import MemoTemplate
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

PROMPT_NAME: Final = "memo_format_v1"
# Spec 0077 §7: вход ≈ 2 тыс. токенов, выход ≈ 1 тыс.; обрыв — один повтор вдвое
# (`complete_whole`), он и покрывает текст на все 6000 знаков поля.
MAX_TOKENS: Final = 1500


def build_request(text: str, template: MemoTemplate | None) -> gateway.LlmRequest:
    """Запрос «Оформить» — один на кабинет и на evals."""
    parts = []
    if template is not None:
        parts.append(f'<template title="{template.title}">\n{template.body}\n</template>')
    parts.append(f"<text>\n{text}\n</text>")
    return gateway.LlmRequest(
        system=load_prompt(PROMPT_NAME),
        messages=[gateway.LlmMessage(role="user", content="\n".join(parts))],
        max_tokens=MAX_TOKENS,
        purpose="memos",
    )


async def format_memo(
    text: str,
    template: MemoTemplate | None,
    *,
    provider: gateway.LlmProvider | None = None,
) -> str:
    """Текст автора деловым языком. Внутри `tenant_context` (бюджет отеля, P-4)."""
    response = await gateway.complete_whole(build_request(text, template), provider=provider)
    formatted = response.text.strip()
    if not formatted:
        logger.warning("memo_format_empty_answer")
        raise AppError(
            code=gateway.ERR_AI_PROVIDER_ERROR,
            message="Model returned an empty answer",
            status_code=502,
        )
    if len(formatted) > memos_api.BODY_MAX_CHARS:
        # В поле записки такой текст не сохранить — отдавать его автору незачем.
        logger.warning(
            "memo_format_too_long",
            error_code=gateway.ERR_AI_INCOMPLETE_ANSWER,
            chars_out=len(formatted),
        )
        raise AppError(
            code=gateway.ERR_AI_INCOMPLETE_ANSWER,
            message="Formatted memo does not fit the memo body",
            status_code=502,
        )
    # Без текстов (spec 0077 §8): только размеры и шаблон.
    logger.info(
        "memo_format_done",
        template_key=template.key if template is not None else None,
        chars_in=len(text),
        chars_out=len(formatted),
    )
    return formatted
