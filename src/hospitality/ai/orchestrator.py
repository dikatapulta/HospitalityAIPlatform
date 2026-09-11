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
  Исключение — вызов, на котором гейт уже удовлетворён самим сообщением гостя
  (срочная заявка, `registry.confirmation_waived`): он исполняется сразу
  (ADR-018, spec 0034 §5).
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
from typing import Any, Final

from hospitality.ai.escalation import EscalationContext, EscalationReason
from hospitality.ai.gateway import api as gateway
from hospitality.ai.gateway.api import (
    LlmMessage,
    LlmProvider,
    LlmRequest,
    ToolCall,
    ToolSpec,
)
from hospitality.ai.prompts import load_prompt
from hospitality.ai.tools import registry
from hospitality.ai.tools.base import ActiveRequest, ConfirmationClass, ToolTurnContext
from hospitality.platform.config import (
    HotelFact,
    TenantConfig,
    hotel_facts_total_chars,
    load_tenant_config,
)
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import current_tenant_id

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
# v5 (spec 0036, issue #333): + раздел «What you know about this hotel» —
# правила чтения блока Hotel facts (справочник отеля из конфига тенанта,
# добавляет оркестратор): отвечать из факта; числа, коды и пароли —
# дословно; временный факт называть временным; не выводить ответ из соседних
# фактов. Плюс правило смешанного хода: если ход и отвечает на покрытый фактом
# вопрос, и предлагает действие, ответ идёт ПЕРВЫМ внутри
# `confirmation_question` — свободный текст на таком ходу гость не увидит
# (гейт P-9 отдаёт ему именно аргумент инструмента), и без правила ответ
# пропал бы молча.
# Вторая половина v5 (spec 0036 §6, issue #334): ветка «факта нет» — сказать
# честно, указать на РЕСЕПШЕН и на том же ходу вызвать
# `report_unanswered_question`; сотрудника ветка не обещает намеренно (обещать
# некому — #101 открыт, а после #101 обещание сделало бы эскалацией каждый
# неизвестный факт). Тем же изменением переписан первый буллет раздела
# `# What you must not do`: он обещал сотрудника ровно на тех вопросах (цены,
# правила, часы), которые теперь отвечает раздел знаний, — два указания на один
# вопрос, а приоритета у правил промпта нет.
PROMPT_NAME = "concierge_v5"
# v2 (spec 0025): реплика подтверждения генерализована под второе действие —
# «заявка отменена», а не только «передана службе» (v1 писался под создание).
CONFIRMATION_PROMPT_NAME = "confirmation_gate_v2"

# Служебный инструмент гейта P-9 — НЕ AI-способность: в реестр (§7.3) не входит,
# сервисов ядра не вызывает. Модель обязана вызвать его на ходе подтверждения.
CONFIRMATION_TOOL_NAME = "resolve_confirmation"

# Служебный сигнал «в справочнике отеля нет ответа» (spec 0036 §6, issue #334) —
# тот же класс, что и вердикт гейта выше: сервиса ядра не зовёт, в отеле ничего
# не меняет, в реестр §7.3 не входит, схему собирает оркестратор, исполнения нет.
# Детерминированного признака «модель не знала ответа» в системе нет — текстовая
# реплика структурно неотличима от любой другой, — поэтому признак объявляется
# контрактом (P-7).
UNANSWERED_QUESTION_TOOL_NAME = "report_unanswered_question"

# Предел длины строки справочника. Одно число на три места (P-12): `maxLength`
# схемы, обрезка разбора и колонка `unanswered_questions.question` VARCHAR(200).
UNANSWERED_QUESTION_MAX_CHARS: Final = 200

# Резервные реплики на случай, если модель не дала текста (обычно даёт — промпт
# и схема классификатора его требуют). Русский — язык демо-тенанта; в норме
# язык реплики задаёт модель по языку гостя. Резерв исполненного действия —
# у модуля инструмента (`DONE_TEXT`): «передаю в службу» для создания было бы
# ложью для отмены (spec 0025).
_ESCALATION_TEXT = "Секунду, я подключу сотрудника отеля."
_DECLINED_TEXT = "Хорошо, ничего не оформляю."
# Последний рубеж реплики сигнала (spec 0036 §6): модель не дала ни аргумента,
# ни свободного текста. Сотрудника не обещает намеренно — обещать некому (#101),
# а после #101 обещание сделало бы эскалацией каждый неизвестный факт (§6).
_UNANSWERED_TEXT = "Этого нет в моей справке — на ресепшене подскажут точно."


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
class _UnansweredSignal:
    """Разобранные вызовы `report_unanswered_question` одного хода (spec 0036 §6).

    `question` — строка для справочника (None, если модель нарушила контракт и
    текста вопроса не дала: писать в список нечего, но гость реплику получает).
    `reply_to_guest` — реплика только из АРГУМЕНТОВ и может быть пустой: запасную
    ступень выбирает `_signal_reply` по исходу хода, потому что свободный текст
    модели годится гостю не на каждом исходе.
    """

    question: str | None
    reply_to_guest: str


@dataclass(frozen=True)
class OrchestratorTurn:
    """Типизированный результат обработки сообщения (P-7).

    Инвариант: `escalation` задан ⟺ `kind is NEEDS_HUMAN` — вызывающая сторона
    (канал) обязана донести факт до персонала (spec 0022), иначе «зову
    сотрудника» — ложь (issue #36).

    `unanswered_question` — вопрос гостя, на который в справочнике отеля не
    нашлось факта (spec 0036 §6). Заполняется на ЛЮБОМ исходе, где модель
    вызвала сигнал, — включая эскалацию: справочник пополняется независимо от
    того, чем кончился ход. Строку пишет канал, буква в букву как `escalation`
    выше: таблица лежит в `channels/common` рядом с `conversation_escalations`
    (P-12, R-10). Машинной проверки этой границы нет — направление
    `ai → channels` контрактом импорт-линтера не закрыто (сиблинги через «:» в
    контракте слоёв `pyproject.toml`); держит канон, а не линтер.
    """

    kind: TurnKind
    reply_text: str
    pending_action: PendingAction | None = None
    created_request_id: uuid.UUID | None = None
    escalation: EscalationContext | None = None
    unanswered_question: str | None = None


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
    # Конфиг тенанта читается РОВНО ОДИН раз на ход и разъезжается по двум
    # потребителям: справочник отеля — в системный промпт, подсказки служб —
    # в описание инструмента (issue #123). До spec 0036 его читал сам реестр
    # инструментов, и второй потребитель дал бы второй одинаковый запрос к БД
    # на каждую реплику гостя.
    config = await _load_config_for_turn()
    facts = config.active_hotel_facts() if config is not None else ()
    # Прибор §9 спеки 0036, парный к строке выше: видно и то, что справочник
    # доехал до промпта, и во что он обходится. Второе не бюрократия — ход со
    # справочником дорожает почти вдвое (§4), а потолок `LLM_TENANT_DAILY_
    # BUDGET_USD` у тенанта один на всё: упёршись в него, отель замолкает
    # (ERR-AI-002). Знаки считает `hotel_facts_total_chars` — та же арифметика,
    # что у предела схемы и у счётчика страницы (P-12).
    logger.info("hotel_facts_in_context", count=len(facts), chars=hotel_facts_total_chars(facts))
    facts_block = _hotel_facts_block(facts)
    request = LlmRequest(
        messages=[*(history or []), LlmMessage(role="user", content=message)],
        # Порядок блоков значим: справочник отеля стабилен у тенанта сутками и
        # стоит СРАЗУ после файла промпта, до блоков хода (комната, заявки).
        # У Anthropic кэшируется префикс до отметки `cache_control`, а всё, что
        # меняется каждый ход, обязано лежать после неё (#138).
        system=load_prompt(PROMPT_NAME)
        + facts_block
        + _verified_room_block(context.verified_room_number)
        + _active_requests_block(context.active_requests)
        + _guest_language_reminder(facts_block),
        tools=[
            *await registry.build_tool_specs(context, config),
            # Сигнал — не из реестра (§7.3): сервиса ядра он не зовёт. Объявлен
            # на КАЖДОМ обычном ходу, в том числе у отеля с пустым справочником:
            # там не покрыт фактом любой вопрос, и список вопросов — единственный
            # способ узнать, чем справочник заполнять (spec 0036 §6).
            _unanswered_question_tool_spec(),
        ],
    )
    # AppError провайдера (ERR-AI-001/002/003) пробрасывается — деградацию при
    # недоступности LLM обрабатывает канал (§7.8), а не оркестратор.
    response = await gateway.complete(request, provider=provider)

    # Сигнал «факта нет» вынимается из списка вызовов ДО выбора действия и
    # независимо от позиции: он никогда не выигрывает у действия (spec 0036 §6).
    signal, tool_calls = _take_unanswered_signal(response.tool_calls)
    question = None if signal is None else signal.question

    if not tool_calls:
        if signal is not None:
            # Ход остаётся REPLY: ничего не исполнено, подтверждать нечего.
            # Реплика — из аргумента сигнала, а не из свободного текста модели
            # (приоритет — `_signal_reply`).
            return OrchestratorTurn(
                kind=TurnKind.REPLY,
                reply_text=_signal_reply(signal, response.text),
                unanswered_question=question,
            )
        # Нет вызова инструмента: обычный ответ (в т.ч. модель словами эскалировала).
        return OrchestratorTurn(kind=TurnKind.REPLY, reply_text=response.text)

    # Phase 0: один инструмент за ход (первый ОСТАВШИЙСЯ). Мультивызовы — Phase 1.
    tool_call = tool_calls[0]

    try:
        declared_class = registry.confirmation_class(tool_call.name)
        # Гейт P-9 может быть уже удовлетворён самим сообщением гостя — срочная
        # заявка исполняется без вопроса «оформить?» (ADR-018, spec 0034 §5).
        # Снятие считается ТОЛЬКО для объявленного класса `confirm_guest`: это
        # граница ADR-018, и она обязана стоять в коде, а не в намерении автора
        # инструмента. У `confirm_staff` (NG-4) снимать нечего и никогда;
        # у `auto` снятие ничего не ускорило бы, но отбросило бы свободный текст
        # модели ниже — а для `auto` он и есть весь ответ гостю (ревью PR #291).
        gate_waived = declared_class is ConfirmationClass.CONFIRM_GUEST and (
            registry.confirmation_waived(tool_call.name, tool_call.arguments)
        )
    except AppError as error:
        # Модель вызвала неизвестный инструмент — не исполняем, эскалируем.
        logger.warning("unknown_tool_call", tool=tool_call.name, code=error.code)
        return OrchestratorTurn(
            kind=TurnKind.NEEDS_HUMAN,
            # Исключение §6: ход, кончившийся эскалацией, отдаёт гостю только
            # текст эскалации — человек уже идёт, «этого нет в справке» перед
            # этим шум. Строка справочника пишется всё равно.
            reply_text=_ESCALATION_TEXT,
            escalation=_escalation_context(
                EscalationReason.UNKNOWN_TOOL, error.code, tool_call.name, tool_call.arguments
            ),
            unanswered_question=question,
        )

    if declared_class is ConfirmationClass.CONFIRM_GUEST and not gate_waived:
        # Гейт P-9: не исполняем на первом предложении — переспрашиваем гостя.
        logger.info("tool_awaiting_confirmation", tool=tool_call.name)
        return OrchestratorTurn(
            kind=TurnKind.AWAITING_CONFIRMATION,
            reply_text=_with_signal_reply(
                signal, _confirmation_prompt(tool_call.arguments, response.text), response.text
            ),
            pending_action=PendingAction(tool_name=tool_call.name, arguments=tool_call.arguments),
            unanswered_question=question,
        )

    # Класс auto или снятый гейт — исполняем сразу. На снятом гейте свободный
    # текст модели гостю не показывается: промпт требует от неё на этом ходу
    # вопроса-подтверждения, а вопрос «оформить заявку?» о УЖЕ созданной заявке
    # был бы ложью. Реплику в таком случае даёт сам инструмент (spec 0034 §5).
    if gate_waived:
        logger.info("confirmation_gate_waived", tool=tool_call.name)
    return await _execute_tool(
        tool_name=tool_call.name,
        arguments=tool_call.arguments,
        context=context,
        reply_text="" if gate_waived else response.text,
        signal=signal,
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


def _unanswered_question_tool_spec() -> ToolSpec:
    """Схема служебного сигнала «в справочнике нет ответа» (spec 0036 §6, P-7).

    Канон — `_confirmation_tool_spec` выше: схему собирает оркестратор, в
    реестре инструментов (§7.3) сигнала нет, исполнения у него нет тоже.

    Описания полей — ПО-АНГЛИЙСКИ, в отличие от боевых инструментов реестра.
    Это не вкус: `reply_to_guest` — текст, который гость прочитает, а язык
    аргумента тянется за языком его описания (замер, issue #342: русские
    описания `create_service_request` дают гостю русский `confirmation_question`
    примерно раз из четырёх). Новое поле гостевого текста заводить с этим
    дефектом незачем.

    «Весь ответ — в `reply_to_guest`» и «один вызов на ход» — не стиль, а два
    замеренных отказа (ревью PR #344, Sonnet 5). Ход «пароль Wi-Fi и есть ли
    утюг» без действия: модель писала ответ из факта свободным текстом, а в
    аргумент клала только «про утюг не знаю» — гость терял ответ 17 раз из 20.
    Ход «есть ли прачечная и где аптека»: два вызова сигнала на два вопроса,
    второй терялся. От второго вызова страхует и разбор
    (`_take_unanswered_signal`), а первый отказ в коде не лечится: свободный
    текст рядом с сигналом гостю не показывается намеренно (приоритет §6).

    Замер доработки (11.09.2026): первая её редакция — одна фраза «this is the
    ONLY text» в описании аргумента плюс оговорка «на ходу с действием клади
    сюда только то, на что факта нет» — дала на русском 5 из 10. Нынешняя —
    правило повторено в описании инструмента, аргумент открывается словами
    «Your whole reply», оговорка без «только» — 26 из 26 на ru/kk/en. Какая из
    трёх правок решила дело, замер не разделял: менять их поодиночке — значит
    мерить заново.
    """
    return ToolSpec(
        name=UNANSWERED_QUESTION_TOOL_NAME,
        description=(
            "Report that the hotel directory has no fact covering something the "
            "guest asked, and say what the guest is told. Call it on the same turn "
            "on which you tell the guest you do not have this information, and at "
            "most once per turn: if several questions are not covered, report all "
            "of them in this one call. On a turn with no other tool, the guest sees "
            "only `reply_to_guest`, so your whole answer goes there — including "
            "every part of the message that a hotel fact does answer. It notifies "
            "nobody and changes nothing in the hotel: it only adds the question to "
            "the list the hotel manager reads to fill the directory in."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "maxLength": UNANSWERED_QUESTION_MAX_CHARS,
                    "description": (
                        "The guest's question that no fact covers, in one short line, "
                        "in the guest's own language — the hotel manager reads it as "
                        "written. If several questions are not covered, put all of them "
                        "into this one line. Leave out anything a fact does answer, "
                        "greetings, and any personal detail (name, room number)."
                    ),
                },
                "reply_to_guest": {
                    "type": "string",
                    "description": (
                        "Your whole reply to the guest, in the guest's own language. On "
                        "a turn where you call no other tool, this is the ONLY text the "
                        "guest sees — anything you write outside it is not shown — so "
                        "it must answer the whole message: first every part a hotel "
                        "fact covers, with the values exactly as written, then say "
                        "plainly that you do not have the rest of the information and "
                        "point the guest to the reception desk. Never promise to ask, "
                        "check with or bring in a member of staff. If this same turn "
                        "also calls an action tool, the system shows this text above "
                        "that tool's confirmation question: do not repeat that question "
                        "here."
                    ),
                },
            },
            "required": ["question", "reply_to_guest"],
        },
    )


def _take_unanswered_signal(
    tool_calls: Sequence[ToolCall],
) -> tuple[_UnansweredSignal | None, list[ToolCall]]:
    """Вынуть сигнал из вызовов хода — НЕЗАВИСИМО от позиции (spec 0036 §6).

    Сигнал никогда не выигрывает у действия: исполняется `tool_calls[0]`, и
    «есть ли утюг и принесите полотенца» потеряло бы заявку из-за того, что
    модель поставила логирование первым вызовом. Возвращает разобранный сигнал
    и ОСТАВШИЕСЯ вызовы — с ними ход работает ровно как раньше.

    Вынимаются и разбираются ВСЕ вызовы сигнала, а не первый. Схема велит звать
    его раз за ход, но это просьба к модели: на «есть ли прачечная и где аптека»
    Sonnet 5 звал его дважды, по вызову на вопрос (ревью PR #344). Отбросить
    второй — потерять вопрос и молча, и дважды: гость не получил бы ответа, а
    менеджер строки; оставить его в списке — отдать ход в `unknown_tool`.
    """
    signal_calls = [call for call in tool_calls if call.name == UNANSWERED_QUESTION_TOOL_NAME]
    remaining = [call for call in tool_calls if call.name != UNANSWERED_QUESTION_TOOL_NAME]
    if not signal_calls:
        return None, remaining
    if len(signal_calls) > 1:
        logger.warning("unanswered_question_signal_repeated", calls=len(signal_calls))
    return _parse_unanswered_signals(signal_calls), remaining


def _parse_unanswered_signals(calls: Sequence[ToolCall]) -> _UnansweredSignal:
    """Аргументы вызовов сигнала → строка справочника и реплика гостю (§6).

    Несколько вызовов сводятся к тому же виду, что и один вызов по схеме: все
    вопросы — одной строкой справочника, различающиеся реплики — абзацами по
    порядку. Одинаковые не повторяются.

    Вопрос режется по пределу схемы: `maxLength` — просьба к модели, а не
    гарантия провайдера, а колонка `unanswered_questions.question` короче
    предела не станет. Текст вопроса в логи не попадает — это текст гостя
    (docs/PII_REGISTRY.md).
    """
    questions = _distinct_arguments(calls, "question")
    question = " / ".join(questions)[:UNANSWERED_QUESTION_MAX_CHARS].strip()
    if question:
        logger.info("unanswered_question_reported")
    else:
        # Поле обязательно схемой — пустое значит нарушенный контракт. Реплику
        # гостю отдаём всё равно: писать нечего только в справочник.
        logger.warning("unanswered_question_without_text")
    return _UnansweredSignal(
        question=question or None,
        reply_to_guest="\n\n".join(_distinct_arguments(calls, "reply_to_guest")),
    )


def _distinct_arguments(calls: Sequence[ToolCall], key: str) -> list[str]:
    """Непустые значения аргумента по всем вызовам, без повторов, по порядку."""
    values = (_argument_str(call.arguments, key) for call in calls)
    return list(dict.fromkeys(value for value in values if value is not None))


def _signal_reply(signal: _UnansweredSignal, model_text: str) -> str:
    """Реплика сигнала гостю: аргумент → свободный текст модели → заглушка (§6).

    Реплика берётся из АРГУМЕНТА, а не из свободного текста: модель часто отдаёт
    tool_use с пустым текстом, и гость получил бы молчание. Третья ступень —
    статическая заглушка на языке демо-тенанта, канон `_ESCALATION_TEXT`; у
    `_confirmation_prompt` она другая (вопрос из `summary`), общие с ним только
    первые две. `model_text` вызывающий передаёт пустым там, где свободный текст
    гостю показывать нельзя, — на снятом гейте (`_execute_tool`).
    """
    return signal.reply_to_guest or model_text.strip() or _UNANSWERED_TEXT


def _with_signal_reply(signal: _UnansweredSignal | None, reply: str, model_text: str) -> str:
    """Реплика сигнала — первым абзацем перед репликой хода (spec 0036 §6).

    Своей репликой сигнал реплику хода не заменяет и молча не исчезает: гость
    получает обе части, ответ первым, через пустую строку. Склейку делает
    оркестратор, а не модель, — поэтому промпт и велит ей не повторять ответ
    внутри `confirmation_question` (§8.2).
    """
    if signal is None:
        return reply
    signal_reply = _signal_reply(signal, model_text)
    if reply in ("", signal_reply):
        # Пустая реплика хода, либо оба рубежа взяли один и тот же свободный
        # текст модели (аргументов не дала ни та сторона, ни другая), — второй
        # раз его показывать незачем.
        return signal_reply
    return f"{signal_reply}\n\n{reply}"


async def _execute_tool(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    context: ToolTurnContext,
    reply_text: str,
    signal: _UnansweredSignal | None = None,
) -> OrchestratorTurn:
    """Исполнить инструмент; ошибка исполнения — эскалация, не 5xx (ERR-AI-004).

    Пустая реплика модели/классификатора — резерв `DONE_TEXT` самого инструмента
    (создание и отмена подтверждаются разными словами, spec 0025).
    `created_request_id` заполняется только создающими инструментами: по нему
    канал пишет привязку `request_origins` — отмена её не создаёт.

    `signal` — сигнал «факта нет» того же хода (spec 0036 §6); на ходе
    подтверждения его не бывает (там `forced_tool` классификатора), поэтому
    умолчание None. Реплика сигнала едет первым абзацем перед репликой хода —
    кроме исхода-эскалации, где гость получает только текст эскалации. Запасной
    ступенью реплики сигнала служит тот же `reply_text`: на снятом гейте он
    пуст, и свободный текст модели («оформить заявку?» о уже созданной заявке)
    не просачивается к гостю и через сигнал — там встаёт заглушка.
    """
    question = None if signal is None else signal.question
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
            unanswered_question=question,
        )

    logger.info("tool_executed", tool=tool_name, request_id=str(result.id))
    return OrchestratorTurn(
        kind=TurnKind.ACTION_DONE,
        reply_text=_with_signal_reply(
            signal, reply_text or registry.done_text(tool_name, arguments), reply_text
        ),
        created_request_id=result.id if registry.creates_request(tool_name) else None,
        unanswered_question=question,
    )


async def _load_config_for_turn() -> TenantConfig | None:
    """Конфиг тенанта на этот ход; недоступен — None (деградация, не отказ).

    Онбординг не завершён или конфиг дрейфнул — ход идёт без справочника отеля
    и без подсказок служб, с WARNING в лог: диалог гостя ценнее и того, и
    другого (та же деградация, что у маршрутизации уведомлений в
    `channels/telegram/routing.py`; до spec 0036 она жила в `_category_hints`).
    """
    try:
        async with session_scope() as session:
            return await load_tenant_config(session, current_tenant_id())
    except AppError as error:
        logger.warning("tenant_config_unavailable", error_code=error.code)
        return None


def _hotel_facts_block(facts: Sequence[HotelFact]) -> str:
    """Блок «справочник отеля» к системному промпту (spec 0036 §4).

    Динамический контекст по канону `_active_requests_block`, но, в отличие от
    него, стабильный: место блока — до блоков хода (см. сборку запроса выше).
    Как и канон, принимает уже готовые данные: правило «просрочен» живёт в
    `active_hotel_facts` (владелец правила и границы по поясу), и зовётся оно
    РОВНО ОДИН раз за ход — выше, там же, где считается лог; два вызова были бы
    двумя разными ответами на границе суток.
    Порядок строк — порядок хранения (его расставил отель). Фактов нет или все
    просрочены — блока нет вовсе, как у пустого списка заявок: промпт v5 велит
    не выдумывать факты, если блока не было.

    Хвост про язык — сверх текста спеки §4, и вот почему. Справочник написан на
    языке отеля, и он перетягивает язык ОТВЕТА: правило «отвечай на языке
    гостя» стоит первой строкой файла промпта с v2, но три килобайта чужого
    языка после него перевешивают. Замер 07.09.2026 (Sonnet 5, четыре
    англоязычных сценария evals): без оговорок гость-англичанин получал ответ
    по-русски в 3 случаях из 4; с абстрактным хвостом («translate the wording
    into the guest's language») — 2–3 из 4, то есть правило не удерживало.
    Держит РАБОЧИЙ ПРИМЕР: с ним 10.09.2026 три простых сценария из трёх ушли
    гостю по-английски на трёх прогонах подряд. Пример показывает переход
    «факт → ответ», а не повторяет запрет, и его языки зашиты жёстко — учить
    надо переходу, и справочник отеля тут ни при чём.

    Ход с вызовом инструмента этим не лечится: там текст гостю живёт в
    `confirmation_question`, а описание самого аргумента написано по-русски.
    Утечка аргумента ЭТИМ PR не внесена — воспроизведена при полностью
    отключённом блоке фактов (issue #342); второй рубеж — блок
    `_guest_language_reminder` ниже.
    """
    if not facts:
        return ""
    lines = [
        "",
        "",
        "# Hotel facts",
        "",
        "Written by this hotel's own staff. This is your only source of truth about",
        "the hotel itself.",
        "",
    ]
    for fact in facts:
        # Пометка временного факта — по-английски и машинно однообразно: её
        # читает модель, и правило промпта v5 опознаёт ровно эту форму.
        temporary = "" if fact.valid_until is None else f" (temporary, until {fact.valid_until})"
        lines.append(f"- {fact.topic}{temporary}: {fact.answer}")
    lines += [
        "",
        "The lines above are written in the hotel's own language. That language says",
        "nothing about the guest, and answering in it because a fact is written in it",
        "is a mistake. Work out the language of the guest's last message, then write",
        "the whole answer in that language, keeping the values (numbers, times,",
        "prices, codes, passwords, network and place names) exactly as written above.",
        "",
        # Рабочий пример, а не ещё одна формулировка правила: абстрактный запрет
        # («translate the wording») модель на этом ходу измеримо не удерживал,
        # показанное преобразование — удержало. Языки примера жёстко зашиты и
        # не зависят от языка справочника: он учит ПЕРЕХОДУ, а не языку.
        'Example: from "Завтрак: с 07:00 до 10:30 на 2 этаже" an English guest must be',
        'told "Breakfast is from 07:00 to 10:30 on the 2nd floor", a Kazakh guest',
        '"Таңғы ас 2-қабатта 07:00–10:30", a Russian guest the Russian sentence.',
    ]
    return "\n".join(lines)


def _guest_language_reminder(facts_block: str) -> str:
    """Правило языка ПОСЛЕДНЕЙ строкой системного промпта (spec 0036 §5).

    Второй рубеж той же утечки, что описана в `_hotel_facts_block`, и держат
    его позиция И формулировка — измерены обе. Позиция: оговорка внутри блока
    фактов измеримо вернула на язык гостя простые ответы, но не ход, где
    реплика гостю живёт в аргументе инструмента (`confirmation_question` модель
    пишет в конце, дальше всего от правила), поэтому напоминание стоит после
    блоков хода — ближе к реплике гостя, чем что-либо ещё в промпте.
    Формулировка решает, удержит ли правило вообще, — замер ниже.

    ЗАМЕРЕН 10.09.2026, и первая редакция блока замера не выдержала: с ней
    гость-англичанин получал ответ по-русски в 2–3 сценариях из 4 (три прогона
    по четыре сценария на Sonnet 5). Нынешняя редакция называет ошибку прямо
    («самая частая ошибка этого хода») и требует определить язык ДО первого
    слова — с ней остаётся 0–1 из 4, и оставшийся провал всегда один и тот же:
    смешанный ход, где текст гостю живёт в аргументе инструмента (issue #342,
    дефект старше этого PR — воспроизведён без блока фактов вовсе).

    Появляется ТОЛЬКО вместе с блоком фактов: у отеля с пустым справочником
    системный промпт обязан остаться байт в байт прежним (DoD issue #333), да и
    утекать там нечему. Цена — ≈170 токенов за ход (`messages.count_tokens`,
    Sonnet 5), и они за отметкой `cache_control` (#138): блок статический, но
    стоит после блоков хода, поэтому кэш их не удешевит никогда. Хвост правила
    языка в блоке фактов — ещё ≈220, уже в кэшируемом префиксе; всё правило
    языка — ≈390 за ход (числа и разбор — spec 0036 §8.2).
    """
    if not facts_block:
        return ""
    return (
        "\n\n# Before you reply\n\n"
        "The guest's LAST message is the only thing that sets the language of your "
        "answer. Before writing a single word, decide what language it is in, and "
        "write every word the guest will read in that language — your reply and "
        "every tool argument (`confirmation_question` above all). The hotel facts "
        "above are reference data, not an example of how to speak: answering in "
        "their language when the guest wrote in another is the single most common "
        "mistake on this turn. Copy values from a fact exactly as written (numbers, "
        "times, prices, codes, passwords, network and place names); translate "
        "everything around them."
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
