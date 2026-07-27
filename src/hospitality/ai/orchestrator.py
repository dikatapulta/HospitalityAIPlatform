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
(ERR-AI-004 и т.п.) оркестратор превращает в эскалацию к человеку, а не в 5xx:
исход `NEEDS_HUMAN` несёт `EscalationContext` (spec 0022, issue #36) — по нему
канал публикует `conversation.escalated`, и staff-чат реально узнаёт о госте.
"""

from __future__ import annotations

import enum
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from hospitality.ai.escalation import EscalationContext, EscalationReason
from hospitality.ai.gateway import api as gateway
from hospitality.ai.gateway.api import LlmMessage, LlmProvider, LlmRequest, ToolSpec
from hospitality.ai.prompts import load_prompt
from hospitality.ai.tools import registry
from hospitality.ai.tools.base import ActiveRequest, ConfirmationClass, ToolTurnContext
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
# v4 (spec 0025, issue #40): + блок Active service requests (снапшот открытых
# заявок диалога, добавляет оркестратор): статус — из списка, просьба, уже
# покрытая открытой заявкой, — не дубль; отмена — только инструментом
# cancel_service_request и только для заявок из списка.
PROMPT_NAME = "concierge_v4"
# v2 (spec 0025): реплика подтверждения генерализована под второе действие —
# «заявка отменена», а не только «передана службе» (v1 писался под создание).
CONFIRMATION_PROMPT_NAME = "confirmation_gate_v2"

# Служебный инструмент гейта P-9 — НЕ AI-способность: в реестр (§7.3) не входит,
# сервисов ядра не вызывает. Модель обязана вызвать его на ходе подтверждения.
CONFIRMATION_TOOL_NAME = "resolve_confirmation"

# Резервные реплики на случай, если модель не дала текста (обычно даёт — промпт
# и схема классификатора его требуют). Русский — язык демо-тенанта; в норме
# язык реплики задаёт модель по языку гостя. Резерв исполненного действия —
# у модуля инструмента (`DONE_TEXT`): «передаю в службу» для создания было бы
# ложью для отмены (spec 0025).
_ESCALATION_TEXT = "Секунду, я подключу сотрудника отеля."
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
    """Типизированный результат обработки сообщения (P-7).

    Инвариант: `escalation` задан ⟺ `kind is NEEDS_HUMAN` — вызывающая сторона
    (канал) обязана донести факт до персонала (spec 0022), иначе «зову
    сотрудника» — ложь (issue #36).
    """

    kind: TurnKind
    reply_text: str
    pending_action: PendingAction | None = None
    created_request_id: uuid.UUID | None = None
    escalation: EscalationContext | None = None


async def handle_message(
    *,
    message: str,
    history: list[LlmMessage] | None = None,
    pending_action: PendingAction | None = None,
    active_requests: Sequence[ActiveRequest] = (),
    verified_room_number: str | None = None,
    provider: LlmProvider | None = None,
) -> OrchestratorTurn:
    """Обработать сообщение гостя (внутри `tenant_context`, P-4).

    `history` — прежние реплики диалога (их хранит вызывающая сторона).
    `pending_action` — предложенное на прошлом ходу действие, ждущее
    подтверждения гостя: если передано, ход трактуется как ответ на
    подтверждение. `active_requests` — снапшот открытых заявок этого диалога
    (spec 0025): канал резолвит их по своей привязке `request_origins` и
    передаёт КАЖДЫЙ ход — по нему модель отвечает о статусе, не плодит дубли,
    а инструмент отмены получает и валидирует допустимые id.
    `verified_room_number` — комната из привязки канала (веб-чат, spec 0027
    §3.2): попадает в контекст инструментов (перезапись комнаты заявки) и в
    системный промпт (модель не переспрашивает номер). `provider`
    переопределяют тесты и композиция; бизнес-код зовёт без него — боевой
    Anthropic из настроек.
    """
    context = ToolTurnContext(
        active_requests=tuple(active_requests), verified_room_number=verified_room_number
    )
    if pending_action is not None:
        return await _handle_confirmation_reply(
            message=message,
            history=history,
            pending_action=pending_action,
            context=context,
            provider=provider,
        )
    return await _handle_new_request(
        message=message, history=history, context=context, provider=provider
    )


async def _handle_new_request(
    *,
    message: str,
    history: list[LlmMessage] | None,
    context: ToolTurnContext,
    provider: LlmProvider | None,
) -> OrchestratorTurn:
    """Обычный ход: консьерж-промпт + инструменты реестра, гейт на предложении."""
    logger.info("active_requests_in_context", count=len(context.active_requests))
    request = LlmRequest(
        messages=[*(history or []), LlmMessage(role="user", content=message)],
        system=load_prompt(PROMPT_NAME)
        + _verified_room_block(context.verified_room_number)
        + _active_requests_block(context.active_requests),
        tools=await registry.build_tool_specs(context),
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
        return OrchestratorTurn(
            kind=TurnKind.NEEDS_HUMAN,
            reply_text=_ESCALATION_TEXT,
            escalation=_escalation_context(
                EscalationReason.UNKNOWN_TOOL, error.code, tool_call.name, tool_call.arguments
            ),
        )

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
        context=context,
        reply_text=response.text,
    )


async def _handle_confirmation_reply(
    *,
    message: str,
    history: list[LlmMessage] | None,
    pending_action: PendingAction,
    context: ToolTurnContext,
    provider: LlmProvider | None,
) -> OrchestratorTurn:
    """Ход подтверждения: структурный вердикт → детерминированное исполнение.

    Действие исполняется из СОХРАНЁННОГО `pending_action`, не завися от того,
    повторит ли модель вызов инструмента (issue #31). `context` — снапшот
    ТЕКУЩЕГО хода: по нему исполнение отмены валидирует id заявки заново.
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
            context=context,
            reply_text=reply,
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
    return await _handle_new_request(
        message=message, history=history, context=context, provider=provider
    )


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
                        "For confirm: acknowledge that the pending action has been carried "
                        "out — match it (a new request: it has been passed to hotel staff; "
                        "a cancellation: the request has been cancelled). For decline: "
                        "acknowledge that nothing was done. For other: leave empty."
                    ),
                },
            },
            "required": ["decision", "reply"],
        },
    )


async def _execute_tool(
    *, tool_name: str, arguments: dict[str, Any], context: ToolTurnContext, reply_text: str
) -> OrchestratorTurn:
    """Исполнить инструмент; ошибка исполнения — эскалация, не 5xx (ERR-AI-004).

    Пустая реплика модели/классификатора — резерв `DONE_TEXT` самого инструмента
    (создание и отмена подтверждаются разными словами, spec 0025).
    `created_request_id` заполняется только создающими инструментами: по нему
    канал пишет привязку `request_origins` — отмена её не создаёт.
    """
    try:
        result = await registry.execute(tool_name, arguments, context)
    except AppError as error:
        logger.warning("tool_execution_failed", tool=tool_name, code=error.code)
        return OrchestratorTurn(
            kind=TurnKind.NEEDS_HUMAN,
            reply_text=_ESCALATION_TEXT,
            escalation=_escalation_context(
                EscalationReason.TOOL_EXECUTION_FAILED, error.code, tool_name, arguments
            ),
        )

    logger.info("tool_executed", tool=tool_name, request_id=str(result.id))
    return OrchestratorTurn(
        kind=TurnKind.ACTION_DONE,
        reply_text=reply_text or registry.done_text(tool_name),
        created_request_id=result.id if registry.creates_request(tool_name) else None,
    )


def _verified_room_block(verified_room_number: str | None) -> str:
    """Блок «подтверждённая комната гостя» к системному промпту (spec 0027 §3.2).

    Динамический контекст, как снапшот заявок (spec 0025), — не новая версия
    файла промпта. Комната пришла из привязки канала (Stay), а не со слов
    гостя: модель не переспрашивает номер, а названную в тексте другую комнату
    всё равно перезапишет инструмент (`ToolTurnContext.verified_room_number`).
    """
    if verified_room_number is None:
        return ""
    return (
        "\n\n# Guest's verified room\n\n"
        f"The guest is verified to be staying in room {verified_room_number} "
        "(confirmed by their check-in access code, not by what they typed). "
        "Do not ask the guest for their room number. Service requests are "
        "always created for this room, even if the guest names another one."
    )


def _active_requests_block(active_requests: tuple[ActiveRequest, ...]) -> str:
    """Блок «открытые заявки диалога» к системному промпту (spec 0025).

    Пустой список — пустая строка (блока нет): промпт v4 велит модели не
    выдумывать заявки, которых нет в списке. `request_id` показывается явно —
    это же значение модель обязана выбрать в enum инструмента отмены (§7.4).
    """
    if not active_requests:
        return ""
    lines = [
        "",
        "",
        "# Active service requests in this conversation",
        "",
        "The system refreshes this list on every turn from the hotel database.",
        "It is the ONLY source of truth about this guest's current requests.",
        "",
    ]
    for request in active_requests:
        number = f"#{request.daily_number}" if request.daily_number is not None else "(no number)"
        room = request.room_number or "-"
        lines.append(
            f"- request_id: {request.id} | {number} | status: {request.status.value} "
            f"| room: {room} | summary: {request.summary}"
        )
    return "\n".join(lines)


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


def _escalation_context(
    reason: EscalationReason, error_code: str, tool_name: str, arguments: dict[str, Any]
) -> EscalationContext:
    """Контекст эскалации из несостоявшегося вызова инструмента (spec 0022).

    `summary`/`room_number` — контракт `create_service_request` (единственный
    боевой инструмент Phase 0); у неизвестного инструмента их обычно нет — поля
    останутся None, персонал увидит последнюю реплику гостя (её несёт канал).
    """
    return EscalationContext(
        reason=reason,
        error_code=error_code,
        tool_name=tool_name,
        action_summary=_argument_str(arguments, "summary"),
        room_number=_argument_str(arguments, "room_number"),
    )


def _argument_str(arguments: dict[str, Any], key: str) -> str | None:
    """Непустое строковое значение аргумента инструмента; иначе None."""
    value = str(arguments.get(key) or "").strip()
    return value or None


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
