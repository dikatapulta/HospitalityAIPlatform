"""Тесты оркестратора на Fake-провайдере (Task 0015/0017.1, R-7).

Краевые случаи поверх golden-set v0 (test_golden_set_v0.py): эскалация при
нарушении контракта инструмента, резервный текст подтверждения и гейт P-9
(Task 0017.1, issue #31): ход подтверждения — структурная классификация
«confirm/decline/other» принудительным вызовом `resolve_confirmation`; на
`confirm` исполняется СОХРАНЁННЫЙ `pending_action`, без ре-эмиссии tool_use.
Полный путь «текст → LLM(mock) → инструмент → сервис requests → заявка» —
в golden-set. Спапшот открытых заявок диалога и отмена гостем — spec 0025.
"""

from __future__ import annotations

import uuid

import pytest

from hospitality.ai import orchestrator, urgency
from hospitality.ai.escalation import EscalationReason
from hospitality.ai.gateway.api import LlmMessage, MockTurn, ScriptedLlmProvider, ToolCall
from hospitality.ai.orchestrator import PendingAction, TurnKind
from hospitality.ai.tools import registry
from hospitality.ai.tools.base import ActiveRequest, ConfirmationClass
from hospitality.modules.requests import api as requests_api
from hospitality.shared.tenancy import tenant_context


async def _request_total() -> int:
    page = await requests_api.list_requests(limit=1, offset=0)
    return page.total


async def _create_request(summary: str = "полотенца в 305") -> requests_api.ServiceRequestRead:
    categories = await requests_api.list_categories()
    category_id = next(c.id for c in categories if c.key == "housekeeping")
    return await requests_api.create_request(
        requests_api.ServiceRequestCreate(category_id=category_id, summary=summary)
    )


def _as_active(request: requests_api.ServiceRequestRead) -> ActiveRequest:
    """Снапшот-представление заявки, как его собирает канал (spec 0025)."""
    return ActiveRequest(
        id=request.id,
        status=request.status,
        summary=request.summary,
        daily_number=request.daily_number,
        room_number=request.room_number,
    )


def _cancel_call(request_id: uuid.UUID) -> ToolCall:
    return ToolCall(
        id="toolu_cancel",
        name="cancel_service_request",
        arguments={
            "request_id": str(request_id),
            "confirmation_question": "Отменить заявку на полотенца?",
        },
    )


def _housekeeping_call(category_key: str = "housekeeping") -> ToolCall:
    return ToolCall(
        id="toolu_1",
        name="create_service_request",
        arguments={
            "category_key": category_key,
            "summary": "убрать номер 305",
            "room_number": "305",
        },
    )


def _urgent_call() -> ToolCall:
    """Вызов инструмента, помеченный моделью срочным (spec 0034 §5)."""
    return ToolCall(
        id="toolu_urgent",
        name="create_service_request",
        arguments={
            "category_key": "engineering",
            "summary": "течёт вода с потолка",
            "room_number": "305",
            "confirmation_question": "Оформить заявку инженерной службе?",
            "guest_language": "ru",
            "is_urgent": True,
        },
    )


def _confirmation_verdict(decision: str, reply: str = "") -> ToolCall:
    """Вердикт классификатора хода подтверждения — провайдер-протокол гейта P-9."""
    return ToolCall(
        id="toolu_verdict",
        name="resolve_confirmation",
        arguments={"decision": decision, "reply": reply},
    )


async def test_confirmation_executes_stored_pending_action_without_reemission(
    demo_tenant: uuid.UUID,
) -> None:
    """Воспроизводящий тест issue #31 (красный до фикса Task 0017.1).

    На ходе подтверждения модель НЕ ре-эмитит `create_service_request` —
    возвращает только структурный вердикт `confirm`. Заявка обязана создаться
    из СОХРАНЁННОГО `pending_action`, а не из повторного tool_use модели.
    """
    provider = ScriptedLlmProvider(
        [
            MockTurn(text="Оформить уборку номера 305 — верно?", tool_calls=[_housekeeping_call()]),
            MockTurn(tool_calls=[_confirmation_verdict("confirm", "Готово, передаю в службу.")]),
        ]
    )
    with tenant_context(demo_tenant):
        proposal = await orchestrator.handle_message(message="уберите номер 305", provider=provider)
        assert proposal.kind is TurnKind.AWAITING_CONFIRMATION
        assert await _request_total() == 0  # до подтверждения заявки нет (P-9)

        history = [
            LlmMessage(role="user", content="уберите номер 305"),
            LlmMessage(role="assistant", content=proposal.reply_text),
        ]
        done = await orchestrator.handle_message(
            message="да",
            history=history,
            pending_action=proposal.pending_action,
            provider=provider,
        )
        assert done.kind is TurnKind.ACTION_DONE
        assert done.created_request_id is not None
        assert done.reply_text == "Готово, передаю в службу."  # язык гостя — от модели
        assert await _request_total() == 1

    # Ход подтверждения — принудительная классификация: единственный служебный
    # инструмент, модель обязана его вызвать (tool_choice forced, не «авось повторит»).
    classification_request = provider.calls[1]
    assert classification_request.forced_tool == "resolve_confirmation"
    assert [tool.name for tool in classification_request.tools] == ["resolve_confirmation"]


async def test_declined_confirmation_creates_nothing_and_clears_gate(
    demo_tenant: uuid.UUID,
) -> None:
    """«Нет» гостя: вердикт `decline` → заявки нет, гейт гаснет (pending пуст)."""
    provider = ScriptedLlmProvider(
        [
            MockTurn(text="Оформить уборку номера 305 — верно?", tool_calls=[_housekeeping_call()]),
            MockTurn(tool_calls=[_confirmation_verdict("decline", "Хорошо, не оформляю.")]),
        ]
    )
    with tenant_context(demo_tenant):
        proposal = await orchestrator.handle_message(message="уберите номер 305", provider=provider)
        declined = await orchestrator.handle_message(
            message="нет, не надо",
            pending_action=proposal.pending_action,
            provider=provider,
        )
        assert declined.kind is TurnKind.REPLY
        assert declined.reply_text == "Хорошо, не оформляю."
        assert declined.pending_action is None  # канал очистит гейт в БД
        assert declined.created_request_id is None
        assert await _request_total() == 0


async def test_changed_mind_is_treated_as_new_request(demo_tenant: uuid.UUID) -> None:
    """Гость передумал (вердикт `other`) → старое предложение снято, сообщение
    обработано как новый запрос: новое предложение с новыми аргументами."""
    revised_call = ToolCall(
        id="toolu_2",
        name="create_service_request",
        arguments={
            "category_key": "engineering",
            "summary": "починить кран",
            "room_number": "305",
        },
    )
    provider = ScriptedLlmProvider(
        [
            MockTurn(text="Оформить уборку номера 305 — верно?", tool_calls=[_housekeeping_call()]),
            MockTurn(tool_calls=[_confirmation_verdict("other")]),
            MockTurn(text="Починить кран в номере 305 — оформить?", tool_calls=[revised_call]),
        ]
    )
    with tenant_context(demo_tenant):
        proposal = await orchestrator.handle_message(message="уберите номер 305", provider=provider)
        revised = await orchestrator.handle_message(
            message="лучше почините кран",
            pending_action=proposal.pending_action,
            provider=provider,
        )
        assert revised.kind is TurnKind.AWAITING_CONFIRMATION
        assert revised.pending_action == PendingAction(
            tool_name="create_service_request", arguments=revised_call.arguments
        )
        assert await _request_total() == 0  # ничего не исполнено без нового «да»


async def test_classifier_protocol_violation_falls_back_to_new_request(
    demo_tenant: uuid.UUID,
) -> None:
    """Фейк нарушил протокол классификации (текст вместо вердикта — боевой
    forced tool choice так ответить не может) → безопасный fallback `other`:
    ничего не исполняется молча, сообщение уходит обычным путём."""
    provider = ScriptedLlmProvider(
        [
            MockTurn(text="Оформить уборку номера 305 — верно?", tool_calls=[_housekeeping_call()]),
            MockTurn(text="Да, конечно!"),  # нарушение: нет вызова resolve_confirmation
            MockTurn(text="Уточните, пожалуйста, что оформить."),
        ]
    )
    with tenant_context(demo_tenant):
        proposal = await orchestrator.handle_message(message="уберите номер 305", provider=provider)
        turn = await orchestrator.handle_message(
            message="да",
            pending_action=proposal.pending_action,
            provider=provider,
        )
        assert turn.kind is TurnKind.REPLY
        assert turn.created_request_id is None
        assert await _request_total() == 0


async def test_unknown_classifier_decision_falls_back_to_new_request(
    demo_tenant: uuid.UUID,
) -> None:
    """Вердикт вне enum (`decision="maybe"`) → безопасный fallback `other`:
    ничего не исполняется молча, сообщение уходит обычным путём."""
    provider = ScriptedLlmProvider(
        [
            MockTurn(text="Оформить уборку номера 305 — верно?", tool_calls=[_housekeeping_call()]),
            MockTurn(tool_calls=[_confirmation_verdict("maybe")]),
            MockTurn(text="Уточните, пожалуйста, что оформить."),
        ]
    )
    with tenant_context(demo_tenant):
        proposal = await orchestrator.handle_message(message="уберите номер 305", provider=provider)
        turn = await orchestrator.handle_message(
            message="да",
            pending_action=proposal.pending_action,
            provider=provider,
        )
        assert turn.kind is TurnKind.REPLY
        assert turn.created_request_id is None
        assert await _request_total() == 0


async def test_unknown_category_key_escalates_and_creates_nothing(demo_tenant: uuid.UUID) -> None:
    # Модель выбрала category_key вне enum тенанта — на исполнении это ERR-AI-004,
    # оркестратор эскалирует к человеку, заявка не создаётся.
    bad_call = _housekeeping_call(category_key="spa")
    provider = ScriptedLlmProvider(
        [MockTurn(tool_calls=[bad_call]), MockTurn(tool_calls=[_confirmation_verdict("confirm")])]
    )

    with tenant_context(demo_tenant):
        first = await orchestrator.handle_message(message="нужно спа", provider=provider)
        assert first.kind is TurnKind.AWAITING_CONFIRMATION
        confirmed = await orchestrator.handle_message(
            message="да",
            pending_action=first.pending_action,
            provider=provider,
        )
        assert confirmed.kind is TurnKind.NEEDS_HUMAN
        assert confirmed.created_request_id is None
        assert await _request_total() == 0
        # Spec 0022: NEEDS_HUMAN несёт контекст — без него канал не донесёт
        # эскалацию до staff-чата (issue #36); суть/комната — из аргументов.
        assert confirmed.escalation is not None
        assert confirmed.escalation.reason is EscalationReason.TOOL_EXECUTION_FAILED
        assert confirmed.escalation.error_code == "ERR-AI-004"
        assert confirmed.escalation.action_summary == "убрать номер 305"
        assert confirmed.escalation.room_number == "305"


async def test_unknown_tool_name_escalates(demo_tenant: uuid.UUID) -> None:
    provider = ScriptedLlmProvider(
        [MockTurn(tool_calls=[ToolCall(id="toolu_x", name="delete_everything", arguments={})])]
    )
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(message="удали всё", provider=provider)
    assert turn.kind is TurnKind.NEEDS_HUMAN
    assert turn.created_request_id is None
    assert turn.escalation is not None  # spec 0022: контекст обязателен при NEEDS_HUMAN
    assert turn.escalation.reason is EscalationReason.UNKNOWN_TOOL
    assert turn.escalation.tool_name == "delete_everything"


async def test_active_requests_snapshot_reaches_model_and_enables_cancel(
    demo_tenant: uuid.UUID,
) -> None:
    """Снапшот открытых заявок диалога — в системном промпте, инструмент отмены —
    в составе хода с enum ровно из id снапшота (spec 0025). DoD «Где моя
    заявка?»: модель отвечает текстом из контекста, без инструмента и персонала.
    """
    provider = ScriptedLlmProvider([MockTurn(text="Ваша заявка #1 уже в работе.")])
    with tenant_context(demo_tenant):
        request = await _create_request()
        turn = await orchestrator.handle_message(
            message="ну что там с полотенцами?",
            active_requests=[_as_active(request)],
            provider=provider,
        )
    assert turn.kind is TurnKind.REPLY
    assert turn.reply_text == "Ваша заявка #1 уже в работе."

    llm_request = provider.calls[0]
    system = llm_request.system or ""
    assert "# Active service requests in this conversation" in system
    assert str(request.id) in system
    assert request.summary in system
    cancel_specs = [tool for tool in llm_request.tools if tool.name == "cancel_service_request"]
    assert len(cancel_specs) == 1
    assert cancel_specs[0].input_schema["properties"]["request_id"]["enum"] == [str(request.id)]


async def test_no_snapshot_means_no_block_and_no_cancel_tool(demo_tenant: uuid.UUID) -> None:
    """Пустой снапшот: блока в промпте нет, инструмента отмены нет — модели
    нечего отменять и не из чего «вспоминать» несуществующие заявки."""
    provider = ScriptedLlmProvider([MockTurn(text="Здравствуйте!")])
    with tenant_context(demo_tenant):
        await orchestrator.handle_message(message="привет", provider=provider)
    llm_request = provider.calls[0]
    system = llm_request.system or ""
    # Сам промпт v4 упоминает блок в правилах («If this prompt contains …»);
    # отсутствие проверяем по ЗАГОЛОВКУ блока и маркеру данных, а не по фразе.
    assert "# Active service requests in this conversation" not in system
    assert "request_id:" not in system
    assert [tool.name for tool in llm_request.tools] == ["create_service_request"]


async def test_cancel_flow_confirms_then_cancels_stored_request(demo_tenant: uuid.UUID) -> None:
    """DoD «Отмените» → подтверждение → `cancelled`: гейт P-9 как у создания;
    `created_request_id` пуст — канал не должен записывать привязку origin."""
    with tenant_context(demo_tenant):
        request = await _create_request()
        snapshot = [_as_active(request)]
        provider = ScriptedLlmProvider(
            [
                MockTurn(tool_calls=[_cancel_call(request.id)]),
                MockTurn(tool_calls=[_confirmation_verdict("confirm", "Отменил вашу заявку.")]),
            ]
        )
        proposal = await orchestrator.handle_message(
            message="отмените, уже не надо",
            active_requests=snapshot,
            provider=provider,
        )
        assert proposal.kind is TurnKind.AWAITING_CONFIRMATION
        assert proposal.reply_text == "Отменить заявку на полотенца?"  # из аргумента (P-9)
        still = await requests_api.get_request(request.id)
        assert still.status is requests_api.RequestStatus.NEW  # до «да» ничего не отменено

        done = await orchestrator.handle_message(
            message="да",
            pending_action=proposal.pending_action,
            active_requests=snapshot,
            provider=provider,
        )
        assert done.kind is TurnKind.ACTION_DONE
        assert done.reply_text == "Отменил вашу заявку."
        assert done.created_request_id is None  # отмена ≠ созданная заявка (spec 0025)
        cancelled = await requests_api.get_request(request.id)
        assert cancelled.status is requests_api.RequestStatus.CANCELLED


async def test_cancel_of_request_outside_snapshot_escalates(demo_tenant: uuid.UUID) -> None:
    """DoD «чужую заявку не отменить»: id вне снапшота ТЕКУЩЕГО хода (чужой
    диалог — или заявку закрыли между ходами) → ERR-AI-004 → NEEDS_HUMAN,
    заявка не тронута."""
    with tenant_context(demo_tenant):
        foreign = await _create_request("чужая заявка")
        provider = ScriptedLlmProvider(
            [
                MockTurn(tool_calls=[_cancel_call(foreign.id)]),
                MockTurn(tool_calls=[_confirmation_verdict("confirm")]),
            ]
        )
        # Снапшот этого диалога пуст: заявка принадлежит другому диалогу.
        proposal = await orchestrator.handle_message(
            message="отмените заявку", active_requests=[], provider=provider
        )
        assert proposal.kind is TurnKind.AWAITING_CONFIRMATION  # гейт вооружился
        confirmed = await orchestrator.handle_message(
            message="да",
            pending_action=proposal.pending_action,
            active_requests=[],
            provider=provider,
        )
        assert confirmed.kind is TurnKind.NEEDS_HUMAN
        assert confirmed.escalation is not None
        assert confirmed.escalation.error_code == "ERR-AI-004"
        untouched = await requests_api.get_request(foreign.id)
        assert untouched.status is requests_api.RequestStatus.NEW


async def test_confirmation_question_argument_is_shown_to_guest(demo_tenant: uuid.UUID) -> None:
    # Обычное поведение модели (замер: Sonnet и Haiku на 6 языках) — tool_use БЕЗ
    # свободного текста, но с заполненным `confirmation_question` на языке гостя.
    # Гость должен увидеть именно этот вопрос (естественный язык), а не заглушку.
    call = ToolCall(
        id="toolu_1",
        name="create_service_request",
        arguments={
            "category_key": "housekeeping",
            "summary": "убрать номер 305",
            "room_number": "305",
            "confirmation_question": "Оформить заявку на уборку номера 305?",
        },
    )
    provider = ScriptedLlmProvider([MockTurn(tool_calls=[call])])  # текста нет — как в проде
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(message="уберите 305", provider=provider)
    assert turn.kind is TurnKind.AWAITING_CONFIRMATION
    assert turn.reply_text == "Оформить заявку на уборку номера 305?"  # из аргумента, не заглушка


async def test_fallback_confirmation_when_model_has_no_text(demo_tenant: uuid.UUID) -> None:
    # Модель вернула вызов инструмента без текста — оркестратор формулирует
    # подтверждающий вопрос из аргументов (резервный путь). Вопрос строится из
    # `summary` (по контракту инструмента — на языке гостя), БЕЗ русских
    # связок «Оформить … Подтвердите»: раньше они утекали чужим языком
    # (Sonnet на казахском вызывал инструмент без текста → русский вопрос гостю).
    provider = ScriptedLlmProvider([MockTurn(tool_calls=[_housekeeping_call()])])
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(message="уберите 305", provider=provider)
    assert turn.kind is TurnKind.AWAITING_CONFIRMATION
    assert turn.pending_action == PendingAction(
        tool_name="create_service_request",
        arguments={
            "category_key": "housekeeping",
            "summary": "убрать номер 305",
            "room_number": "305",
        },
    )
    # Из summary (room «305» уже в нём) — без захардкоженных русских связок.
    assert turn.reply_text == "убрать номер 305?"
    assert "Оформить" not in turn.reply_text and "Подтвердите" not in turn.reply_text


async def test_urgent_request_is_created_without_confirmation(demo_tenant: uuid.UUID) -> None:
    """Срочная заявка исполняется на первом же ходу (ADR-018, spec 0034 §5).

    Воспроизводит находку А-3 аудита: до этой правки гость с аварией получал
    вопрос «оформить?» и заявки не появлялось, пока он не написал «да».
    """
    provider = ScriptedLlmProvider([MockTurn(tool_calls=[_urgent_call()])])
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(
            message="из потолка хлещет вода, заливает номер", provider=provider
        )
        assert turn.kind is TurnKind.ACTION_DONE
        assert turn.pending_action is None
        assert turn.created_request_id is not None
        assert await _request_total() == 1
        created = await requests_api.get_request(turn.created_request_id)
        assert created.is_urgent is True
    # Второго обращения к модели не было: гейт снят, а не пройден.
    assert len(provider.calls) == 1


async def test_urgent_reply_is_static_and_in_guest_language(demo_tenant: uuid.UUID) -> None:
    """На снятом гейте гость слышит утверждённый абзац на своём языке.

    Свободный текст модели на этом ходу — вопрос-подтверждение (его требует
    промпт), и показать его о УЖЕ созданной заявке было бы ложью (spec 0034 §5).
    """
    call = _urgent_call()
    call.arguments["guest_language"] = "en"
    provider = ScriptedLlmProvider([MockTurn(text="Should I submit a request?", tool_calls=[call])])
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(
            message="water is flooding my room", provider=provider
        )
    assert turn.reply_text == urgency.urgent_accepted_reply("en")
    assert "Should I submit" not in turn.reply_text


async def test_non_urgent_request_still_asks_for_confirmation(demo_tenant: uuid.UUID) -> None:
    """Умолчание не меняется: обычная заявка проходит гейт P-9 как прежде."""
    call = _urgent_call()
    call.arguments["is_urgent"] = False
    provider = ScriptedLlmProvider([MockTurn(tool_calls=[call])])
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(message="протекает кран", provider=provider)
        assert turn.kind is TurnKind.AWAITING_CONFIRMATION
        assert await _request_total() == 0


async def test_gate_waiver_applies_only_to_the_declared_confirm_guest_class(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Снятие гейта считается только у объявленного `confirm_guest` (ADR-018).

    Щит от ловушки, которую заводит новый контракт (ревью PR #291): автор
    инструмента класса `auto` естественно напишет `confirmation_waived → True`
    («подтверждения не требую») — и до этой правки его свободный текст молча
    отбрасывался вместе с гейтом, хотя для `auto` он и есть весь ответ гостю.
    Для `confirm_staff` (NG-4) граница та же: снимать нечем и никогда.
    """
    monkeypatch.setattr(registry, "confirmation_class", lambda tool_name: ConfirmationClass.AUTO)
    provider = ScriptedLlmProvider(
        [MockTurn(text="Готово, инженер уже вышел.", tool_calls=[_urgent_call()])]
    )
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(message="течёт вода", provider=provider)
        assert turn.kind is TurnKind.ACTION_DONE
        assert turn.reply_text == "Готово, инженер уже вышел."


async def test_urgent_flag_from_model_must_be_a_real_true(demo_tenant: uuid.UUID) -> None:
    """Строка «true» — не «да»: безопасная сторона тут гейт (spec 0034 §5).

    Одно правило на два пути: гейт читает сырые аргументы, запись в БД — схему,
    и разойтись они не имеют права.
    """
    call = _urgent_call()
    call.arguments["is_urgent"] = "true"
    provider = ScriptedLlmProvider([MockTurn(tool_calls=[call])])
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(message="протекает кран", provider=provider)
        assert turn.kind is TurnKind.AWAITING_CONFIRMATION
