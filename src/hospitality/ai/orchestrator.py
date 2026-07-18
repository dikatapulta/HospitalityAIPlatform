"""Оркестратор диалога (Task 0015/0017.1, FOUNDATION §7.1).

Единая точка обработки сообщения гостя: собирает запрос (промпт + инструменты
под тенанта), зовёт LLM через `ai/gateway`, исполняет объявленный инструмент и
возвращает типизированный исход. Бизнес-логики не содержит (P-5): создание
заявки живёт в `modules/requests`, оркестратор лишь её вызывает.

Подтверждение (P-9) — структурный гейт, не текст промпта. Два пути:

- Обычный ход (`pending_action is None`): инструмент класса `confirm_guest` НЕ
  исполняется на первом предложении — возвращается `awaiting_confirmation` с
  `pending_action`; вызывающая сторона (канал, Task 0016/0017) хранит его в
  `conversations.pending_action` и передаёт обратно на следующем ходу.
- Ход подтверждения (`pending_action` передан): оркестратор НЕ полагается на
  ре-эмиссию tool_use моделью (баг issue #31 — Haiku повторяет вызов
  нестабильно). Вместо этого — структурная классификация ответа гостя
  принудительным вызовом служебного инструмента `resolve_confirmation`
  (`forced_tool`, свободный текст невозможен): `confirm` → исполнить
  СОХРАНЁННЫЙ `pending_action` (tool_name + arguments); `decline` → гейт
  гаснет; `other` (передумал/правка) → сообщение обрабатывается как новый
  запрос. Детали — docs/specs/0017.1-deterministic-confirmation.md.

Ошибки провайдера (`AppError` ERR-AI-001/002/003) НЕ глотаются — деградация при
недоступности LLM (§7.8) — забота канала. Ошибку исполнения инструмента
(ERR-AI-004 и т.п.) оркестратор превращает в эскалацию к человеку, а не в 5xx.
"""

from __future__ import annotations

import enum
import json
import uuid
from dataclasses import dataclass
from typing import Any

from hospitality.ai.gateway import api as gateway
from hospitality.ai.gateway.api import LlmMessage, LlmProvider, LlmRequest, ToolSpec
from hospitality.ai.prompts import load_prompt
from hospitality.ai.tools import registry
from hospitality.ai.tools.base import ConfirmationClass
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Версия промпта — в имени файла (§7.5). Смена версии — отдельная строка + evals.
# v2 (Task 0017.1): промпт на английском, жёсткое правило языка первой строкой,
# предложение действия — всегда вопрос (2 дефекта bake-off'а, DISCUSSION_LOG).
# v3 (баг #71): v2 учил модель, что вызов инструмента = отправка заявки службе
# («submitted after the guest confirms»), поэтому Haiku придерживал tool_use до
# «да» — а на ходе «да» pending_action не было, и заявка не создавалась никогда
# (на английском воспроизводилось 0/4, гейт не вооружался). v3 переформулирует:
# вызов инструмента лишь ЧЕРНОВИК (ничего не отправляет — это делает система
# после подтверждения), поэтому модель обязана звать инструмент на том же ходу,
# где предлагает заявку. Замер на Haiku: v2 en 0/4, kk 3/4 → v3 24/24 (6 языков).
PROMPT_NAME = "concierge_v3"
CONFIRMATION_PROMPT_NAME = "confirmation_gate_v1"

# Служебный инструмент гейта P-9 — НЕ AI-способность: в реестр (§7.3) не входит,
# сервисов ядра не вызывает. Модель обязана вызвать его на ходе подтверждения.
CONFIRMATION_TOOL_NAME = "resolve_confirmation"

# Резервные реплики на случай, если модель не дала текста (обычно даёт — промпт
# и схема классификатора его требуют). Русский — язык демо-тенанта; в норме
# язык реплики задаёт модель по языку гостя.
_ESCALATION_TEXT = "Секунду, я подключу сотрудника отеля."
_DONE_TEXT = "Готово, передаю в службу отеля."
_DECLINED_TEXT = "Хорошо, ничего не оформляю."


class TurnKind(enum.StrEnum):
    """Исход обработки одного сообщения гостя."""

    REPLY = "reply"  # обычный текстовый ответ (в т.ч. модель сама эскалировала словами)
    AWAITING_CONFIRMATION = "awaiting_confirmation"  # предложено действие, ждём «да» гостя
    ACTION_DONE = "action_done"  # инструмент исполнен, заявка создана
    NEEDS_HUMAN = "needs_human"  # не смогли исполнить — передаём сотруднику


class ConfirmationDecision(enum.StrEnum):
    """Структурный вердикт классификатора на ходе подтверждения (гейт P-9)."""

    CONFIRM = "confirm"  # гость подтвердил — исполнить сохранённое действие
    DECLINE = "decline"  # гость отказался — гейт гаснет, ничего не исполняется
    OTHER = "other"  # передумал/правка/другая тема — обработать как новый запрос


@dataclass(frozen=True)
class PendingAction:
    """Предложенный, но не исполненный вызов инструмента (гейт P-9).

    Хранится вызывающей стороной между ходами; его наличие на следующем ходу —
    сигнал «гость отвечает на подтверждение». На `confirm` исполняются именно
    эти сохранённые `tool_name` + `arguments` — не пересказ модели.
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

    `history` — прежние реплики диалога (их хранит вызывающая сторона).
    `pending_action` — предложенное на прошлом ходу действие, ждущее
    подтверждения гостя: если передано, ход трактуется как ответ на
    подтверждение. `provider` переопределяют тесты и композиция; бизнес-код
    зовёт без него — боевой Anthropic из настроек.
    """
    if pending_action is not None:
        return await _handle_confirmation_reply(
            message=message,
            history=history,
            pending_action=pending_action,
            provider=provider,
        )
    return await _handle_new_request(message=message, history=history, provider=provider)


async def _handle_new_request(
    *,
    message: str,
    history: list[LlmMessage] | None,
    provider: LlmProvider | None,
) -> OrchestratorTurn:
    """Обычный ход: консьерж-промпт + инструменты реестра, гейт на предложении."""
    request = LlmRequest(
        messages=[*(history or []), LlmMessage(role="user", content=message)],
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

    if needs_confirmation:
        # Гейт P-9: не исполняем на первом предложении — переспрашиваем гостя.
        logger.info("tool_awaiting_confirmation", tool=tool_call.name)
        return OrchestratorTurn(
            kind=TurnKind.AWAITING_CONFIRMATION,
            reply_text=_confirmation_prompt(tool_call.arguments, response.text),
            pending_action=PendingAction(tool_name=tool_call.name, arguments=tool_call.arguments),
        )

    # Класс auto — исполняем сразу.
    return await _execute_tool(
        tool_name=tool_call.name,
        arguments=tool_call.arguments,
        reply_text=response.text or _DONE_TEXT,
    )


async def _handle_confirmation_reply(
    *,
    message: str,
    history: list[LlmMessage] | None,
    pending_action: PendingAction,
    provider: LlmProvider | None,
) -> OrchestratorTurn:
    """Ход подтверждения: структурный вердикт → детерминированное исполнение.

    Заявка создаётся из СОХРАНЁННОГО `pending_action`, не завися от того,
    повторит ли модель вызов инструмента (issue #31).
    """
    decision, reply = await _classify_confirmation(
        message=message,
        history=history,
        pending_action=pending_action,
        provider=provider,
    )

    if decision is ConfirmationDecision.CONFIRM:
        logger.info("pending_action_confirmed", tool=pending_action.tool_name)
        return await _execute_tool(
            tool_name=pending_action.tool_name,
            arguments=pending_action.arguments,
            reply_text=reply or _DONE_TEXT,
        )

    if decision is ConfirmationDecision.DECLINE:
        # Гейт гаснет: канал очищает pending_action на всех исходах, кроме
        # AWAITING_CONFIRMATION. Ничего не исполняется.
        logger.info("pending_action_declined", tool=pending_action.tool_name)
        return OrchestratorTurn(kind=TurnKind.REPLY, reply_text=reply or _DECLINED_TEXT)

    # OTHER: гость передумал/сменил тему — старое предложение снимается (безопасная
    # сторона P-9: потерянное предложение гость повторит, лишнее исполнение — нет),
    # сообщение обрабатывается как новый запрос.
    logger.info("pending_action_superseded", tool=pending_action.tool_name)
    return await _handle_new_request(message=message, history=history, provider=provider)


async def _classify_confirmation(
    *,
    message: str,
    history: list[LlmMessage] | None,
    pending_action: PendingAction,
    provider: LlmProvider | None,
) -> tuple[ConfirmationDecision, str]:
    """Структурная классификация ответа гостя: forced tool — текст невозможен.

    Возвращает вердикт и короткую реплику гостю на его языке (`reply`
    классификатора). Нарушение протокола (нет вердикта в ответе — реальный API
    с `forced_tool` так не отвечает) — безопасный fallback `OTHER`.
    """
    pending_summary = json.dumps(
        {"tool": pending_action.tool_name, "arguments": pending_action.arguments},
        ensure_ascii=False,
        sort_keys=True,
    )
    request = LlmRequest(
        messages=[*(history or []), LlmMessage(role="user", content=message)],
        system=(
            load_prompt(CONFIRMATION_PROMPT_NAME)
            + "\n\n# Pending action awaiting the guest's confirmation\n"
            + pending_summary
        ),
        tools=[_confirmation_tool_spec()],
        forced_tool=CONFIRMATION_TOOL_NAME,
    )
    response = await gateway.complete(request, provider=provider)

    verdict = next(
        (call for call in response.tool_calls if call.name == CONFIRMATION_TOOL_NAME), None
    )
    if verdict is None:
        logger.warning("confirmation_classifier_protocol_violation")
        return ConfirmationDecision.OTHER, ""
    try:
        decision = ConfirmationDecision(str(verdict.arguments.get("decision", "")))
    except ValueError:
        logger.warning(
            "confirmation_classifier_unknown_decision",
            decision=verdict.arguments.get("decision"),
        )
        return ConfirmationDecision.OTHER, ""
    return decision, str(verdict.arguments.get("reply") or "").strip()


def _confirmation_tool_spec() -> ToolSpec:
    """Схема служебного вердикта — контракт классификации (P-7)."""
    return ToolSpec(
        name=CONFIRMATION_TOOL_NAME,
        description=(
            "Classify the guest's reply to the pending confirmation question "
            "and produce a short reply to the guest."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": [decision.value for decision in ConfirmationDecision],
                    "description": (
                        "confirm — the guest clearly agrees to proceed with the pending "
                        "action as is; decline — the guest clearly refuses it; other — "
                        "anything else (changed details, new request, unrelated message)."
                    ),
                },
                "reply": {
                    "type": "string",
                    "description": (
                        "Short reply to the guest, written in the guest's own language. "
                        "For confirm: acknowledge the request has been passed to hotel "
                        "staff. For decline: acknowledge the cancellation. For other: "
                        "leave empty."
                    ),
                },
            },
            "required": ["decision", "reply"],
        },
    )


async def _execute_tool(
    *, tool_name: str, arguments: dict[str, Any], reply_text: str
) -> OrchestratorTurn:
    """Исполнить инструмент; ошибка исполнения — эскалация, не 5xx (ERR-AI-004)."""
    try:
        result = await registry.execute(tool_name, arguments)
    except AppError as error:
        logger.warning("tool_execution_failed", tool=tool_name, code=error.code)
        return OrchestratorTurn(kind=TurnKind.NEEDS_HUMAN, reply_text=_ESCALATION_TEXT)

    logger.info("tool_executed", tool=tool_name, request_id=str(result.id))
    return OrchestratorTurn(
        kind=TurnKind.ACTION_DONE,
        reply_text=reply_text,
        created_request_id=result.id,
    )


def _confirmation_prompt(arguments: dict[str, Any], model_text: str) -> str:
    """Вопрос-подтверждение гостю на ходе AWAITING_CONFIRMATION (гейт P-9).

    Источник — поле `confirmation_question` инструмента: модель почти всегда
    зовёт инструмент без свободного текста (замер: Sonnet и Haiku на 6 языках
    дают tool_use с пустым `text`), но аргументы заполняет надёжно и на языке
    гостя. Приоритет: аргумент → свободный текст модели (если вдруг есть) →
    оборонительная заглушка из `summary` (почти недостижима: поле обязательно
    схемой инструмента).
    """
    question = str(arguments.get("confirmation_question") or "").strip()
    return question or model_text.strip() or _fallback_confirmation(arguments)


def _fallback_confirmation(arguments: dict[str, Any]) -> str:
    """Последняя линия обороны, если модель не дала ни `confirmation_question`,
    ни свободного текста (почти недостижимо: поле обязательно схемой).

    Язык гостя здесь без ещё одного вызова LLM неизвестен, поэтому вопрос строим
    из `summary` (по контракту инструмента — уже на языке гостя) плюс номер и «?».
    Не идеальная грамматика, но без чужого языка.
    """
    summary = str(arguments.get("summary") or "").strip()
    room = str(arguments.get("room_number") or "").strip()
    if not summary:  # summary обязателен схемой (min_length=1) — путь оборонительный
        return "OK?"
    return f"{summary} — {room}?" if room and room not in summary else f"{summary}?"
