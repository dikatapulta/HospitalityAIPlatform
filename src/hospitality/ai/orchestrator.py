"""Оркестратор диалога (Task 0015, FOUNDATION §7.1).

Единая точка обработки сообщения гостя: собирает запрос (промпт + инструменты
под тенанта), зовёт LLM через `ai/gateway`, исполняет объявленный инструмент и
возвращает типизированный исход. Бизнес-логики не содержит (P-5): создание
заявки живёт в `modules/requests`, оркестратор лишь её вызывает.

Подтверждение (P-9) — структурный гейт, не текст промпта: инструмент класса
`confirm_guest` НЕ исполняется на первом предложении. Оркестратор возвращает
`awaiting_confirmation` с `pending_action`; вызывающая сторона (канал, Task 0016)
хранит его в состоянии диалога и передаёт обратно на следующем ходу. Когда гость
подтвердил — модель повторяет вызов, и, раз `pending_action` передан, инструмент
исполняется.

Ошибки провайдера (`AppError` ERR-AI-001/002/003) НЕ глотаются — деградация при
недоступности LLM (§7.8) — забота канала. Ошибку исполнения инструмента
(ERR-AI-004 и т.п.) оркестратор превращает в эскалацию к человеку, а не в 5xx.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from typing import Any

from hospitality.ai.gateway import api as gateway
from hospitality.ai.gateway.api import LlmMessage, LlmProvider, LlmRequest
from hospitality.ai.prompts import load_prompt
from hospitality.ai.tools import registry
from hospitality.ai.tools.base import ConfirmationClass
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Версия промпта — в имени файла (§7.5). Смена версии — отдельная строка + evals.
PROMPT_NAME = "concierge_v1"

# Резервные реплики на случай, если модель вернула вызов инструмента без текста
# (обычно текст есть — промпт просит его). Русский — язык демо-тенанта; в норме
# язык реплики задаёт модель по языку гостя.
_ESCALATION_TEXT = "Секунду, я подключу сотрудника отеля."
_DONE_TEXT = "Готово, передаю в службу отеля."


class TurnKind(enum.StrEnum):
    """Исход обработки одного сообщения гостя."""

    REPLY = "reply"  # обычный текстовый ответ (в т.ч. модель сама эскалировала словами)
    AWAITING_CONFIRMATION = "awaiting_confirmation"  # предложено действие, ждём «да» гостя
    ACTION_DONE = "action_done"  # инструмент исполнен, заявка создана
    NEEDS_HUMAN = "needs_human"  # не смогли исполнить — передаём сотруднику


@dataclass(frozen=True)
class PendingAction:
    """Предложенный, но не исполненный вызов инструмента (гейт P-9).

    Хранится вызывающей стороной между ходами; его наличие на следующем ходу —
    сигнал «гость отвечает на подтверждение», по которому оркестратор исполняет
    повторный вызов инструмента.
    """

    tool_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class OrchestratorTurn:
    """Типизированный результат обработки сообщения (P-7)."""

    kind: TurnKind
    reply_text: str
    pending_action: PendingAction | None = None
    created_request_id: uuid.UUID | None = None


async def handle_message(
    *,
    message: str,
    history: list[LlmMessage] | None = None,
    pending_action: PendingAction | None = None,
    provider: LlmProvider | None = None,
) -> OrchestratorTurn:
    """Обработать сообщение гостя (внутри `tenant_context`, P-4).

    `history` — прежние реплики диалога (в Phase 0 их хранит вызывающая сторона;
    Conversation/Message появятся в Task 0016). `pending_action` — предложенное
    на прошлом ходу действие, ждущее подтверждения гостя. `provider` переопределяют
    тесты и композиция; бизнес-код зовёт без него — боевой Anthropic из настроек.
    """
    conversation = [*(history or []), LlmMessage(role="user", content=message)]
    request = LlmRequest(
        messages=conversation,
        system=load_prompt(PROMPT_NAME),
        tools=await registry.build_tool_specs(),
    )
    # AppError провайдера (ERR-AI-001/002/003) пробрасывается — деградацию при
    # недоступности LLM обрабатывает канал (§7.8), а не оркестратор.
    response = await gateway.complete(request, provider=provider)

    if not response.tool_calls:
        # Нет вызова инструмента: обычный ответ (в т.ч. модель словами эскалировала).
        return OrchestratorTurn(kind=TurnKind.REPLY, reply_text=response.text)

    # Phase 0: один инструмент за ход (первый). Мультивызовы — Phase 1.
    tool_call = response.tool_calls[0]

    try:
        needs_confirmation = (
            registry.confirmation_class(tool_call.name) is ConfirmationClass.CONFIRM_GUEST
        )
    except AppError as error:
        # Модель вызвала неизвестный инструмент — не исполняем, эскалируем.
        logger.warning("unknown_tool_call", tool=tool_call.name, code=error.code)
        return OrchestratorTurn(kind=TurnKind.NEEDS_HUMAN, reply_text=_ESCALATION_TEXT)

    if needs_confirmation and pending_action is None:
        # Гейт P-9: не исполняем на первом предложении — переспрашиваем гостя.
        logger.info("tool_awaiting_confirmation", tool=tool_call.name)
        return OrchestratorTurn(
            kind=TurnKind.AWAITING_CONFIRMATION,
            reply_text=response.text or _fallback_confirmation(tool_call.arguments),
            pending_action=PendingAction(tool_name=tool_call.name, arguments=tool_call.arguments),
        )

    # Класс auto, либо гость подтвердил (pending_action передан) — исполняем.
    try:
        result = await registry.execute(tool_call.name, tool_call.arguments)
    except AppError as error:
        logger.warning("tool_execution_failed", tool=tool_call.name, code=error.code)
        return OrchestratorTurn(kind=TurnKind.NEEDS_HUMAN, reply_text=_ESCALATION_TEXT)

    logger.info("tool_executed", tool=tool_call.name, request_id=str(result.id))
    return OrchestratorTurn(
        kind=TurnKind.ACTION_DONE,
        reply_text=response.text or _DONE_TEXT,
        created_request_id=result.id,
    )


def _fallback_confirmation(arguments: dict[str, Any]) -> str:
    """Подтверждающий вопрос, если модель не приложила текст (редко)."""
    summary = str(arguments.get("summary") or "").strip()
    room = str(arguments.get("room_number") or "").strip()
    where = f" (номер {room})" if room else ""
    core = summary or "заявку"
    return f"Оформить {core}{where}? Подтвердите, пожалуйста."
