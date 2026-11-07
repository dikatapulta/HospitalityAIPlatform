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
from hospitality.ai.tools.base import (
    ActiveRequest,
    ConfirmationClass,
    PreparedCall,
    ToolTurnContext,
)

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
# Язык резерва неизвестен — kk, ru, en с новой строки, как у канона (#224).
DONE_TEXT: Final = (
    "Дайын, өтінім болдырылмады.\nГотово, заявка отменена.\nDone — the request is cancelled."
)


def confirmation_waived(arguments: Mapping[str, Any]) -> bool:
    """Снят ли гейт P-9 на этом вызове (контракт модуля инструмента, ADR-018).

    У отмены — никогда. Срочной отмены не бывает: спешка здесь ничего не
    спасает, а ошибочно отменённая заявка не видна ни гостю, ни службе, пока
    её не хватятся (ADR-018, «границы снятия»).
    """
    return False


async def prepare(arguments: dict[str, Any], context: ToolTurnContext) -> PreparedCall:
    """Вызов модели → вызов для гейта P-9 (контракт модуля, spec 0056 §4).

    Отмене до вопроса проверять нечего: id она проверяет на исполнении, по
    снапшоту хода «да». Аргументы — как есть, вопрос — модели.
    """
    return PreparedCall(arguments=arguments)


def done_text(arguments: Mapping[str, Any]) -> str:
    """Резервная реплика «исполнено» под аргументы вызова (контракт модуля).

    У отмены она одна на все вызовы — аргументы на неё не влияют.
    """
    return DONE_TEXT


def done_text_is_final(arguments: Mapping[str, Any]) -> bool:
    """Реплика классификатора на «да» годится гостю (контракт модуля, spec 0056 §4)."""
    return False


# Причина отмены для персонала и аудита (resolution_note заявки). Фиксированная
# русская строка: свободная причина от гостя — вне Phase 0 (spec 0025).
CANCELLED_BY_GUEST_NOTE: Final = "Отменена гостем через чат."

# Описания — по-английски, как у `escalate_to_staff` и `remind_about_request`:
# язык реплики тянется за языком описания (#342). Прогон evals spec 0070 (PR
# #875): на вопрос о заявке, которой нет в списке, англичанин получил ответ
# по-русски — почти дословный пересказ прежней русской фразы «честно скажи, что
# не видишь такой заявки, и предложи позвать сотрудника».
_DESCRIPTION = (
    "Cancel one of the guest's active requests when the guest asks to cancel it or "
    "says the service is no longer needed. Pick request_id ONLY from the allowed "
    "values (the enum) — these are this guest's requests from the Active service "
    "requests block. If the request is not in the list, do not call this tool: tell "
    "the guest, in the language of their last message, that you do not see such a "
    "request in this chat and offer to bring in a staff member."
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
                        "The id of the request to cancel — strictly one of the allowed "
                        "values (see the Active service requests block)."
                    ),
                },
                "confirmation_question": {
                    "type": "string",
                    "description": (
                        "One short, polite question to the guest, in the language of "
                        "the guest's last message, asking whether to cancel exactly "
                        "this request. Always NAME the request in the question (and its "
                        "#N number, if present), so that the guest confirms the right "
                        "request instead of guessing. A question about a future action "
                        "— never a statement that it is already cancelled."
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
