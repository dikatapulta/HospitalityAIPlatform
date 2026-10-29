"""Реквизиты для АВР: разбор запроса, затем фото — моделью (spec 0049 Р-4, Р-13; #530).

Подписчик `act_request.submitted` в медленной полосе воркера (регистрирует
`worker.py`). Два шага одной доставки, по порядку: разбор текста гостя и
PDF/DOCX без модели (`documents.extract_requisites`), затем фото — моделью через
шлюз (назначение `documents`: своя модель и свой дневной бюджет, ADR-024).
Порядок важен: модель заполняет только поля, которые разбор оставил пустыми.

Модель выключена (`LLM_MODEL_DOCUMENTS` пуст) — второго шага нет, поля фото
заполняет бухгалтер, как до PR 5. Отказ шлюза (бюджет, таймаут, ошибка
провайдера) — WARNING и тот же исход: черновик — подсказка, а не обязательство,
и ради него событие не повторяется.

Ответ модели — только вызов инструмента `record_requisites` (`forced_tool`):
свободного текста, который кто-то прочтёт, нет; значения — данные гостя, не
инструкция (FOUNDATION §7.6), формат и место в строке решает домен
(`documents.apply_scan_requisites`).
"""

from __future__ import annotations

import uuid
from typing import Final, Literal, cast, get_args

from hospitality.ai.gateway import api as gateway
from hospitality.ai.gateway.api import (
    LlmImage,
    LlmMessage,
    LlmProvider,
    LlmRequest,
    LlmResponse,
    ToolSpec,
)
from hospitality.ai.prompts import load_prompt
from hospitality.modules.documents import api as documents_api
from hospitality.shared.config import get_settings
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Версия промпта — в имени файла (§7.5).
PROMPT_NAME: Final = "act_requisites_scan_v1"
TOOL_NAME: Final = "record_requisites"
# Шесть коротких строк — сотни токенов; запас на длинные название и адрес.
_MAX_TOKENS: Final = 1024
_USER_TEXT: Final = "Read the company details from the attached photos and call record_requisites."

_MediaType = Literal["image/jpeg", "image/png", "image/webp"]
_MEDIA_TYPES: Final = frozenset(get_args(_MediaType))

RequisiteField = documents_api.RequisiteField

RECORD_TOOL: Final = ToolSpec(
    name=TOOL_NAME,
    description=(
        "Record the company details read from the photos. Use an empty string for any "
        "field that is not clearly visible."
    ),
    input_schema={
        "type": "object",
        "properties": {
            field.value: {"type": "string", "description": description}
            for field, description in (
                (RequisiteField.COMPANY_NAME, "Legal name with its legal form, as printed."),
                (RequisiteField.BIN_IIN, "BIN or IIN: 12 digits."),
                (
                    RequisiteField.IIK,
                    "Bank account (IBAN): KZ, 2 digits, 16 Latin letters or digits.",
                ),
                (RequisiteField.BIK, "Bank code (BIC): 8 Latin letters or digits."),
                (RequisiteField.BANK_NAME, "Bank name, as printed."),
                (RequisiteField.ADDRESS, "Legal address, as printed."),
            )
        },
        "required": [field.value for field in RequisiteField],
        "additionalProperties": False,
    },
)


async def handle_act_request_submitted(event: documents_api.ActRequestSubmitted) -> None:
    """Подписчик воркера: разбор без модели, затем фото — моделью."""
    await documents_api.extract_requisites(event.act_request_id)
    await read_requisites_from_scans(event.act_request_id)


async def read_requisites_from_scans(
    act_request_id: uuid.UUID, *, provider: LlmProvider | None = None
) -> int:
    """Черновик полей по фото запроса; вернуть, сколько полей заполнено.

    Зовётся внутри `tenant_context` (доставка события его ставит). `provider`
    переопределяют тесты, боевой — модель назначения `documents`.
    """
    if not get_settings().llm_model_documents:
        return 0
    scans = [
        scan
        for scan in await documents_api.scan_images(act_request_id)
        if scan.content_type in _MEDIA_TYPES
    ]
    if not scans:
        return 0
    request = build_scan_request(
        [
            LlmImage(media_type=cast("_MediaType", scan.content_type), data=scan.data)
            for scan in scans
        ]
    )
    try:
        response = await gateway.complete(request, provider=provider)
    except AppError as exc:
        logger.warning(
            "act_scans_model_failed", act_request_id=str(act_request_id), error_code=exc.code
        )
        return 0
    values = recorded_values(response)
    if values is None:
        logger.warning("act_scans_no_tool_call", act_request_id=str(act_request_id))
        return 0
    return await documents_api.apply_scan_requisites(
        act_request_id, values, file_ids=[scan.file_id for scan in scans]
    )


def build_scan_request(images: list[LlmImage]) -> LlmRequest:
    """Запрос к модели по фото одного запроса — один на подписчика и офлайн-прогон."""
    return LlmRequest(
        system=load_prompt(PROMPT_NAME),
        messages=[LlmMessage(role="user", content=_USER_TEXT, images=images)],
        tools=[RECORD_TOOL],
        forced_tool=TOOL_NAME,
        max_tokens=_MAX_TOKENS,
        purpose="documents",
    )


def recorded_values(response: LlmResponse) -> dict[RequisiteField, str] | None:
    """Аргументы вызова `record_requisites`; нет вызова — None. Не строки — мимо."""
    call = next((call for call in response.tool_calls if call.name == TOOL_NAME), None)
    if call is None:
        return None
    return {
        field: value
        for field in RequisiteField
        if isinstance(value := call.arguments.get(field.value), str)
    }
