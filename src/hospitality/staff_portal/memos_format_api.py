"""«Оформить» в черновике записки (spec 0077 §7, PR 6 серии; #936): `POST …/api/memos/format`.

Канон `memos_actions_api.py`: CSRF-щит `browser.require_json_same_origin`, личность —
`staff_auth.require_role` любого уровня, раздел выключен у отеля — 404. Затем отметка
согласия автора на Anthropic (`ai_consent.require`, 403 ERR-AUTH-003) — до вызова шлюза;
модель назначения `memos` не задана — ERR-AI-006 (503) от шлюза, до провайдера. Текст —
из поля черновика, а не из базы: оформить можно и несохранённое, сохраняет автор сам. Ответ
модели — только предложение: «Принять» или «Вернуть мой текст» решает автор в браузере,
отправляет — окном подтверждения (P-9). Объяснительная это действие не зовёт — у её
черновика кнопки нет (§7). Ошибки шлюза уходят конвертом с кодом (ERR-AI-001/002/003/006/007),
текст сотруднику по коду выбирает `static/memo_format.js`. В логах — без текстов.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from hospitality.ai import memo_format
from hospitality.ai.gateway import api as gateway
from hospitality.modules.memos import api as memos_api
from hospitality.staff_portal import ai_consent, memos
from hospitality.staff_portal.ai_consent import AiSection
from hospitality.staff_portal.browser import require_json_same_origin

router = APIRouter(prefix="/{tenant_slug}/api/memos", tags=["staff-portal"])


def get_memo_format_provider() -> gateway.LlmProvider | None:
    """Боевой провайдер шлюза (None — по модели назначения); тесты подменяют его
    Fake-адаптером (ADR-007)."""
    return None


FormatProvider = Annotated[gateway.LlmProvider | None, Depends(get_memo_format_provider)]


class FormatBody(BaseModel):
    """Поле черновика: текст (пределы записки) и выбранный шаблон, если есть."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=memos_api.BODY_MAX_CHARS)
    template_key: str | None = Field(default=None, max_length=40)

    @field_validator("text")
    @classmethod
    def _text(cls, value: str) -> str:
        # NUL Postgres не хранит — такой текст автор и не сохранил бы (канон `mentor_api`).
        if "\x00" in value:
            raise ValueError("нулевой символ в тексте")
        text = value.strip()
        if not text:
            raise ValueError("пустой текст")
        return text


@router.post("/format", summary="«Оформить»: текст черновика деловым языком (spec 0077 §7)")
async def format_draft(
    request: Request, body: FormatBody, provider: FormatProvider
) -> dict[str, str]:
    require_json_same_origin(request)
    staff = await memos.any_role(request)
    config = await memos.require_section(staff)
    ai_consent.require(staff, AiSection.MEMO_FORMAT)
    # Неизвестный ключ (шаблон удалили, пока черновик открыт) — как свободная форма.
    template = next((t for t in config.memo_templates if t.key == body.template_key), None)
    text = await memo_format.format_memo(body.text, template, provider=provider)
    return {"text": text}
