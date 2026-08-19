"""Инструмент AI: отменить заявку по просьбе гостя (issue #40, spec 0025, P-5).

Копия канона `create_service_request`: логики нет — контракт и тонкая обёртка
над `modules/requests.change_request_status`. Особенности:

- `request_id` в схеме — enum из id ОТКРЫТЫХ заявок этого диалога (снапшот хода,
  spec 0025): модель выбирает из закрытого списка, а не выдумывает id (§7.4,
  тот же приём, что enum `category_key`). Инструмент включается в состав хода
  только при непустом списке — пустой enum бессмыслен.
- класс подтверждения — `confirm_guest` (P-9): гейт исполнения тот же, что у
  создания — отмена исполняется только после «да» гостя, и срочностью он не
  снимается (`confirmation_waived` всегда False, ADR-018).
- на исполнении id повторно проверяется по снапшоту ТЕКУЩЕГО хода
  (`ToolTurnContext`): чужая заявка, устаревший/подделанный `pending_action`
  или заявка, закрытая персоналом между ходами, — ERR-AI-004 (эскалация),
  а не тихая отмена. Тенантную изоляцию держит RLS (P-4), диалоговую — снапшот.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Final

from pydantic import BaseModel, Field, ValidationError

from hospitality.ai.gateway.api import ToolSpec
from hospitality.ai.tools.base import ActiveRequest, ConfirmationClass, ToolTurnContext

# ERR-AI-004 (docs/runbooks/errors.md, R-8) — общий код «модель нарушила контракт
# инструмента»; определён в каноне create_service_request.
from hospitality.ai.tools.create_service_request import ERR_AI_INVALID_TOOL_CALL
from hospitality.modules.requests import api as requests_api
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

NAME = "cancel_service_request"
CONFIRMATION_CLASS = ConfirmationClass.CONFIRM_GUEST
# Отмена ничего не создаёт: канал не записывает request_origin по этому
# инструменту (spec 0025; поле created_request_id хода остаётся пустым).
CREATES_REQUEST: Final = False
# Резерв «исполнено», если модель не дала текста (в норме даёт, на языке гостя).
DONE_TEXT: Final = "Готово, заявка отменена."


def confirmation_waived(arguments: Mapping[str, Any]) -> bool:
    """Снят ли гейт P-9 на этом вызове (контракт модуля инструмента, ADR-018).

    У отмены — никогда. Срочной отмены не бывает: спешка здесь ничего не
    спасает, а ошибочно отменённая заявка не видна ни гостю, ни службе, пока
    её не хватятся (ADR-018, «границы снятия»).
    """
    return False


def done_text(arguments: Mapping[str, Any]) -> str:
    """Резервная реплика «исполнено» под аргументы вызова (контракт модуля).

    У отмены она одна на все вызовы — аргументы на неё не влияют.
    """
    return DONE_TEXT


# Причина отмены для персонала и аудита (resolution_note заявки). Фиксированная
# русская строка: свободная причина от гостя — вне Phase 0 (spec 0025).
CANCELLED_BY_GUEST_NOTE: Final = "Отменена гостем через чат."

_DESCRIPTION = (
    "Отменить активную заявку гостя, когда он просит её отменить или говорит, "
    "что услуга больше не нужна. Поле request_id бери ТОЛЬКО из списка "
    "допустимых значений (enum) — это заявки текущего гостя из блока Active "
    "service requests. Если нужной заявки в списке нет — не вызывай инструмент, "
    "а честно скажи, что не видишь такой заявки, и предложи позвать сотрудника."
)


class CancelServiceRequestArgs(BaseModel):
    """Аргументы, которые модель передаёт инструменту."""

    request_id: uuid.UUID
    # UX-поле гейта P-9, НЕ персистится (канон create_service_request): вопрос-
    # подтверждение гостю на его языке; оркестратор показывает его на ходе
    # AWAITING_CONFIRMATION. Optional — оборонительно (pending_action без поля).
    confirmation_question: str | None = Field(default=None, max_length=500)


def build_spec(active_requests: tuple[ActiveRequest, ...]) -> ToolSpec:
    """Собрать `ToolSpec` под открытые заявки диалога (спапшот хода, §7.4).

    Вызывается только при непустом списке (состав хода собирает реестр).
    """
    return ToolSpec(
        name=NAME,
        description=_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "enum": [str(request.id) for request in active_requests],
                    "description": (
                        "id отменяемой заявки — строго одно из допустимых значений "
                        "(см. блок Active service requests)."
                    ),
                },
                "confirmation_question": {
                    "type": "string",
                    "description": (
                        "Одна короткая, вежливая фраза-вопрос гостю на ЕГО языке "
                        "(язык последнего сообщения гостя), спрашивающая, отменить ли "
                        "именно эту заявку. Обязательно НАЗОВИ в вопросе суть заявки "
                        "(и её номер #N, если есть), чтобы гость подтвердил отмену "
                        "нужной заявки, а не угадывал. Вопрос о будущем действии — "
                        "никогда не утверждение, что уже отменено."
                    ),
                },
            },
            "required": ["request_id", "confirmation_question"],
        },
    )


async def execute(
    arguments: dict[str, Any], context: ToolTurnContext
) -> requests_api.ServiceRequestRead:
    """Отменить заявку из аргументов модели (внутри `tenant_context`, P-4).

    `request_id` обязан быть в снапшоте открытых заявок ТЕКУЩЕГО хода — иначе
    ERR-AI-004 (модель нарушила контракт / действие устарело). Недопустимый
    переход жизненного цикла (гонка с закрытием персоналом в этот же миг) —
    доменный ERR-REQUESTS-003; оба кода оркестратор превращает в эскалацию.
    """
    try:
        args = CancelServiceRequestArgs.model_validate(arguments)
    except ValidationError as error:
        raise AppError(
            code=ERR_AI_INVALID_TOOL_CALL,
            message="cancel_service_request arguments do not match the tool contract",
            status_code=422,
        ) from error

    if context.find_active_request(args.request_id) is None:
        raise AppError(
            code=ERR_AI_INVALID_TOOL_CALL,
            message=(f"request {args.request_id} is not an active request of this conversation"),
            status_code=422,
        )

    result = await requests_api.change_request_status(
        args.request_id,
        requests_api.RequestStatus.CANCELLED,
        resolution_note=CANCELLED_BY_GUEST_NOTE,
        initiator=requests_api.RequestInitiator.GUEST,
    )
    logger.info(
        "service_request_cancelled_by_guest",
        request_id=str(result.id),
        daily_number=result.daily_number,
    )
    return result
