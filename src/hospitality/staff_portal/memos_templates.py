"""«Шаблоны записок» — экран менеджера (spec 0077 §7, §8; PR 6 серии, #936).

Шаблоны — конфигурация отеля (`TenantConfig.memo_templates`, NG-7); правка — только
`mutate_tenant_config` (SELECT … FOR UPDATE), список пишется целиком: что на экране, то
и станет шаблонами отеля (канон «Вида гостевого чата», `chat_look.py`). Ключ шаблона —
его имя в записках (`memos.template_key`): у прежнего шаблона ключ сохраняется, новому
выдаётся `t` и 8 знаков hex. Удалённый пропадает из выбора в черновике; записи с его
ключом не меняются — модуль сверяет ключ только при правке черновика, и форма черновика
без такого шаблона показывает «Свободную форму».
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from hospitality.platform.config import TenantConfig, mutate_tenant_config
from hospitality.platform.memo_config import (
    MAX_MEMO_TEMPLATES,
    MAX_TEMPLATE_BODY_CHARS,
    MAX_TEMPLATE_TITLE_CHARS,
    MemoTemplate,
)
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.db import platform_session_scope
from hospitality.shared.logging import get_logger
from hospitality.staff_portal import memos
from hospitality.staff_portal import memos_texts as texts

logger = get_logger(module=__name__)


class TemplateInput(BaseModel):
    """Шаблон с экрана: `key` — прежнего шаблона, у нового — None."""

    model_config = ConfigDict(extra="forbid")

    key: str | None = Field(default=None, max_length=40)
    title: str = Field(max_length=MAX_TEMPLATE_TITLE_CHARS)
    body: str = Field(max_length=MAX_TEMPLATE_BODY_CHARS)

    @field_validator("title", "body")
    @classmethod
    def _filled(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("пустое поле")
        return text


class TemplatesInput(BaseModel):
    """Весь список экрана; больше предела — 422, как и у схемы конфига."""

    model_config = ConfigDict(extra="forbid")

    templates: list[TemplateInput] = Field(max_length=MAX_MEMO_TEMPLATES)


def page_context(staff: StaffContext, config: TenantConfig) -> dict[str, Any]:
    """Контекст `memo_templates.html`."""
    return {
        **memos.tabs_context(staff, "templates"),
        "api": f"/staff/{staff.tenant_slug}/api/memos/templates",
        "templates": [
            {"key": template.key, "title": template.title, "body": template.body}
            for template in config.memo_templates
        ],
        "max_templates": MAX_MEMO_TEMPLATES,
        "title_max": MAX_TEMPLATE_TITLE_CHARS,
        "body_max": MAX_TEMPLATE_BODY_CHARS,
        "title": texts.TEMPLATES_TITLE,
        "intro": texts.TEMPLATES_INTRO.format(count=MAX_MEMO_TEMPLATES),
        "hint": texts.TEMPLATES_HINT,
        "name_label": texts.TEMPLATE_NAME,
        "body_label": texts.TEMPLATE_BODY,
        "add_label": texts.TEMPLATE_ADD,
        "delete_label": texts.TEMPLATE_DELETE,
        "save_label": texts.TEMPLATES_SAVE,
        "messages": {
            key: text.format(count=MAX_MEMO_TEMPLATES)
            for key, text in texts.TEMPLATES_MESSAGES.items()
        },
    }


def _new_key(taken: set[str]) -> str:
    while True:
        key = f"t{uuid.uuid4().hex[:8]}"
        if key not in taken:
            return key


def replacing(items: Sequence[TemplateInput]) -> Callable[[TenantConfig], TenantConfig]:
    """Правка для `mutate_tenant_config`: список шаблонов целиком, в порядке экрана.

    Прежний ключ сохраняется, только если такой шаблон есть в конфиге под блокировкой и
    ещё не взят строкой выше; иначе — новый ключ (два окна с одним списком не сольют
    шаблоны в один)."""

    def mutate(config: TenantConfig) -> TenantConfig:
        known = {template.key for template in config.memo_templates}
        taken: set[str] = set()
        result = []
        for item in items:
            key = item.key if item.key in known and item.key not in taken else None
            key = key or _new_key(known | taken)
            taken.add(key)
            result.append(MemoTemplate(key=key, title=item.title, body=item.body))
        return config.model_copy(update={"memo_templates": tuple(result)})

    return mutate


async def save_templates(staff: StaffContext, items: Sequence[TemplateInput]) -> list[str]:
    """«Сохранить»: ключи шаблонов в порядке экрана — экран запомнит ключи новых."""
    async with platform_session_scope() as session:
        updated = await mutate_tenant_config(session, staff.tenant_id, replacing(items))
    # Тексты шаблонов — данные отеля, но в лог не идут: только число и кто (spec 0033 §9).
    logger.info(
        "staff.memo_templates_saved",
        count=len(updated.memo_templates),
        user_id=str(staff.user_id),
    )
    return [template.key for template in updated.memo_templates]
