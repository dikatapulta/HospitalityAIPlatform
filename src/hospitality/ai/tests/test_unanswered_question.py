"""Сигнал «в справочнике нет ответа» в оркестраторе (spec 0036 §6, issue #334).

Проверяется то, чего не видно ни из схемы, ни из промпта: что сигнал никогда не
выигрывает у действия того же хода (иначе «есть ли утюг и принесите полотенца»
теряло бы заявку — исполняется `tool_calls[0]`), что реплика сигнала доезжает до
гостя первым абзацем и что вопрос попадает в исход на ЛЮБОМ конце хода, включая
эскалацию.
"""

from __future__ import annotations

import uuid

from hospitality.ai import orchestrator
from hospitality.ai.gateway.api import MockTurn, ScriptedLlmProvider, ToolCall
from hospitality.ai.orchestrator import TurnKind
from hospitality.modules.requests import api as requests_api
from hospitality.shared.tenancy import tenant_context

SIGNAL_REPLY = "Про утюг в моей справке нет — на ресепшене подскажут точно."
CONFIRMATION = "Оформить заявку на воду в номер 305?"


def _signal_call(
    question: str | None = "есть ли в номере утюг",
    reply_to_guest: str | None = SIGNAL_REPLY,
) -> ToolCall:
    """Вызов служебного сигнала — ровно то, что присылает модель."""
    arguments: dict[str, object] = {}
    if question is not None:
        arguments["question"] = question
    if reply_to_guest is not None:
        arguments["reply_to_guest"] = reply_to_guest
    return ToolCall(
        id="toolu_signal", name=orchestrator.UNANSWERED_QUESTION_TOOL_NAME, arguments=arguments
    )


def _water_call() -> ToolCall:
    """Просьба того же хода: обычная заявка класса CONFIRM_GUEST (гейт P-9 стоит)."""
    return ToolCall(
        id="toolu_water",
        name="create_service_request",
        arguments={
            "category_key": "housekeeping",
            "summary": "принести воду",
            "room_number": "305",
            "confirmation_question": CONFIRMATION,
            "guest_language": "ru",
        },
    )


def _urgent_water_call() -> ToolCall:
    """Тот же ход на СНЯТОМ срочностью гейте (ADR-018, spec 0034 §5)."""
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


async def _request_total() -> int:
    return (await requests_api.list_requests(limit=1, offset=0)).total


async def test_signal_is_offered_to_the_model_on_every_turn(demo_tenant: uuid.UUID) -> None:
    """Сигнал объявлен рядом с боевыми инструментами, но НЕ из реестра (§7.3).

    У этого тенанта справочник пуст — сигнал всё равно объявлен: именно у отеля
    с пустым справочником не покрыт фактом любой вопрос, и список вопросов —
    единственный способ узнать, чем справочник заполнять (§6).
    """
    provider = ScriptedLlmProvider([MockTurn(text="Здравствуйте!")])
    with tenant_context(demo_tenant):
        await orchestrator.handle_message(message="привет", provider=provider)

    declared = [tool.name for tool in provider.calls[0].tools]
    assert orchestrator.UNANSWERED_QUESTION_TOOL_NAME in declared
    # Боевой инструмент никуда не делся, и порядок «сначала реестр» сохранён.
    assert declared[0] == "create_service_request"


async def test_signal_alone_replies_from_its_argument_and_records_the_question(
    demo_tenant: uuid.UUID,
) -> None:
    """Сигнал единственным вызовом: ход остаётся REPLY, реплика — из аргумента.

    Модель почти всегда отдаёт tool_use с пустым текстом (замер на 6 языках), и
    без приоритета «аргумент → текст модели → заглушка» гость получил бы
    молчание. Свободный текст здесь НЕ пуст намеренно: иначе перевёрнутый
    приоритет («текст выше аргумента») остался бы зелёным.
    """
    provider = ScriptedLlmProvider(
        [MockTurn(text="Отмечу этот вопрос для менеджера.", tool_calls=[_signal_call()])]
    )
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(message="есть ли утюг?", provider=provider)

    assert turn.kind is TurnKind.REPLY
    assert turn.reply_text == SIGNAL_REPLY
    assert turn.unanswered_question == "есть ли в номере утюг"
    assert turn.pending_action is None


async def test_signal_without_argument_falls_back_to_model_text_then_to_stub(
    demo_tenant: uuid.UUID,
) -> None:
    """Приоритет реплики: аргумент → свободный текст модели → статическая заглушка."""
    provider = ScriptedLlmProvider(
        [
            MockTurn(text="Такого в справке нет.", tool_calls=[_signal_call(reply_to_guest=None)]),
            MockTurn(tool_calls=[_signal_call(reply_to_guest="")]),
        ]
    )
    with tenant_context(demo_tenant):
        from_model_text = await orchestrator.handle_message(
            message="есть ли утюг?", provider=provider
        )
        from_stub = await orchestrator.handle_message(message="есть ли утюг?", provider=provider)

    assert from_model_text.reply_text == "Такого в справке нет."
    assert from_stub.reply_text == orchestrator._UNANSWERED_TEXT
    # Вопрос записан в обоих случаях: реплика — отдельная забота от справочника.
    assert from_model_text.unanswered_question == "есть ли в номере утюг"
    assert from_stub.unanswered_question == "есть ли в номере утюг"


async def test_signal_without_question_still_replies_but_records_nothing(
    demo_tenant: uuid.UUID,
) -> None:
    """Модель нарушила контракт (нет текста вопроса): гость получает реплику,
    а в справочник писать нечего — строки не будет (канал смотрит на None)."""
    provider = ScriptedLlmProvider([MockTurn(tool_calls=[_signal_call(question="  ")])])
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(message="есть ли утюг?", provider=provider)

    assert turn.kind is TurnKind.REPLY
    assert turn.reply_text == SIGNAL_REPLY
    assert turn.unanswered_question is None


async def test_long_question_is_cut_to_the_schema_limit(demo_tenant: uuid.UUID) -> None:
    """`maxLength` — просьба к модели, а не гарантия провайдера, а колонка
    `unanswered_questions.question` короче предела не станет: режет разбор."""
    provider = ScriptedLlmProvider([MockTurn(tool_calls=[_signal_call(question="я" * 500)])])
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(message="длинный вопрос", provider=provider)

    assert turn.unanswered_question is not None
    assert len(turn.unanswered_question) == orchestrator.UNANSWERED_QUESTION_MAX_CHARS


async def test_signal_first_in_the_list_does_not_lose_the_request(
    demo_tenant: uuid.UUID,
) -> None:
    """Сигнал ПЕРВЫМ вызовом хода (spec 0036 §6): заявка не теряется.

    Сегодня исполняется `tool_calls[0]` — без изъятия сигнала из списка ход
    «есть ли утюг и принесите воду» отдал бы гостю «нет в справке» и потерял
    просьбу молча. Заявки при этом ещё нет: `create_service_request` — класс
    CONFIRM_GUEST, гейт P-9 не снят.
    """
    provider = ScriptedLlmProvider([MockTurn(tool_calls=[_signal_call(), _water_call()])])
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(
            message="есть ли утюг и принесите воду в 305", provider=provider
        )
        assert await _request_total() == 0

    assert turn.kind is TurnKind.AWAITING_CONFIRMATION
    assert turn.pending_action is not None
    assert turn.pending_action.tool_name == "create_service_request"
    assert turn.unanswered_question == "есть ли в номере утюг"
    # Обе части доехали: ответ первым абзацем, вопрос-подтверждение вторым (§6).
    assert turn.reply_text == f"{SIGNAL_REPLY}\n\n{CONFIRMATION}"


async def test_signal_second_in_the_list_gives_the_same_turn(demo_tenant: uuid.UUID) -> None:
    """Та же проверка при обратном порядке вызовов: позиция сигнала ничего не
    решает — оркестратор вынимает его из списка независимо от места."""
    provider = ScriptedLlmProvider([MockTurn(tool_calls=[_water_call(), _signal_call()])])
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(
            message="принесите воду в 305, и есть ли утюг", provider=provider
        )
        assert await _request_total() == 0

    assert turn.kind is TurnKind.AWAITING_CONFIRMATION
    assert turn.pending_action is not None
    assert turn.pending_action.tool_name == "create_service_request"
    assert turn.unanswered_question == "есть ли в номере утюг"
    assert turn.reply_text == f"{SIGNAL_REPLY}\n\n{CONFIRMATION}"


async def test_two_signals_of_different_questions_both_reach_guest_and_manager(
    demo_tenant: uuid.UUID,
) -> None:
    """Два вызова сигнала на два вопроса (замер ревью PR #344: «есть ли прачечная
    и где аптека» — Sonnet 5 звал сигнал дважды в 2 прогонах из 2).

    Схема велит звать его раз за ход, но это просьба к модели. Разбор сводит
    вызовы к виду одного: вопросы — одной строкой справочника, реплики —
    абзацами. Отбрось разбор второй вызов — вопрос про аптеку не получил бы ни
    гость, ни менеджер, и без следа в логе.
    """
    laundry = "Про прачечную в моей справке нет — уточните на ресепшене."
    pharmacy = "Где ближайшая аптека, не подскажу — спросите на ресепшене."
    provider = ScriptedLlmProvider(
        [
            MockTurn(
                tool_calls=[
                    _signal_call("Есть ли в отеле прачечная?", laundry),
                    _signal_call("Где ближайшая аптека?", pharmacy),
                ]
            )
        ]
    )
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(
            message="есть ли прачечная? и где аптека?", provider=provider
        )

    assert turn.kind is TurnKind.REPLY
    assert turn.reply_text == f"{laundry}\n\n{pharmacy}"
    assert turn.unanswered_question == "Есть ли в отеле прачечная? / Где ближайшая аптека?"


async def test_second_signal_does_not_reach_the_tool_registry(demo_tenant: uuid.UUID) -> None:
    """Вынимаются ВСЕ вызовы сигнала, а не первый: второй, оставшись в списке,
    дошёл бы до реестра и отдал ход в ложную эскалацию `unknown_tool` вместо
    вопроса-подтверждения заявки. Одинаковые вызовы не удваивают ни реплику, ни
    строку справочника."""
    provider = ScriptedLlmProvider(
        [MockTurn(tool_calls=[_signal_call(), _signal_call(), _water_call()])]
    )
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(
            message="есть ли утюг и принесите воду в 305", provider=provider
        )

    assert turn.kind is TurnKind.AWAITING_CONFIRMATION
    assert turn.escalation is None
    assert turn.pending_action is not None
    assert turn.pending_action.tool_name == "create_service_request"
    assert turn.unanswered_question == "есть ли в номере утюг"
    assert turn.reply_text == f"{SIGNAL_REPLY}\n\n{CONFIRMATION}"


async def test_same_free_text_on_both_sides_is_shown_once(demo_tenant: uuid.UUID) -> None:
    """Модель не дала аргументов ни сигналу, ни инструменту — оба рубежа берут
    один и тот же свободный текст, и склейка показала бы его дважды."""
    model_text = "Про утюг не знаю — спросите на ресепшене. Принести воду в 305?"
    water_without_question = ToolCall(
        id="toolu_water",
        name="create_service_request",
        arguments={
            "category_key": "housekeeping",
            "summary": "принести воду",
            "room_number": "305",
            "guest_language": "ru",
        },
    )
    provider = ScriptedLlmProvider(
        [
            MockTurn(
                text=model_text,
                tool_calls=[_signal_call(reply_to_guest=None), water_without_question],
            )
        ]
    )
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(
            message="есть ли утюг и принесите воду в 305", provider=provider
        )

    assert turn.kind is TurnKind.AWAITING_CONFIRMATION
    assert turn.reply_text == model_text


async def test_waived_gate_creates_the_request_and_keeps_the_glue(
    demo_tenant: uuid.UUID,
) -> None:
    """Снятый срочностью гейт (ADR-018): заявка создана, склейка та же.

    Реплику на таком ходу даёт сам инструмент (`done_text`), а не модель, —
    реплика сигнала всё равно стоит перед ней первым абзацем.
    """
    provider = ScriptedLlmProvider([MockTurn(tool_calls=[_signal_call(), _urgent_water_call()])])
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(
            message="есть ли утюг и с потолка течёт", provider=provider
        )
        assert await _request_total() == 1

    assert turn.kind is TurnKind.ACTION_DONE
    assert turn.created_request_id is not None
    assert turn.unanswered_question == "есть ли в номере утюг"
    assert turn.reply_text.startswith(f"{SIGNAL_REPLY}\n\n")
    assert turn.reply_text != f"{SIGNAL_REPLY}\n\n"  # вторая часть непустая


async def test_waived_gate_never_shows_free_text_even_through_the_signal(
    demo_tenant: uuid.UUID,
) -> None:
    """На снятом гейте свободный текст модели гостю не показывается: промпт
    велит ей писать вопрос «оформить?», а заявка уже создана — вопрос был бы
    ложью. Пустой `reply_to_guest` не должен протащить этот текст запасной
    ступенью сигнала: там встаёт заглушка (ревью PR #344)."""
    model_text = "Оформить заявку инженерной службе?"
    provider = ScriptedLlmProvider(
        [
            MockTurn(
                text=model_text,
                tool_calls=[_signal_call(reply_to_guest=""), _urgent_water_call()],
            )
        ]
    )
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(
            message="есть ли утюг и с потолка течёт", provider=provider
        )
        assert await _request_total() == 1

    assert turn.kind is TurnKind.ACTION_DONE
    assert model_text not in turn.reply_text
    assert turn.reply_text.startswith(f"{orchestrator._UNANSWERED_TEXT}\n\n")


async def test_escalation_by_unknown_tool_keeps_the_question_but_not_the_reply(
    demo_tenant: uuid.UUID,
) -> None:
    """Исключение §6: ход, кончившийся эскалацией, отдаёт гостю ТОЛЬКО текст
    эскалации — человек уже идёт, «этого нет в справке» перед этим шум. Строка
    справочника пишется всё равно: он пополняется независимо от исхода хода."""
    unknown = ToolCall(id="toolu_x", name="order_taxi", arguments={"destination": "аэропорт"})
    provider = ScriptedLlmProvider([MockTurn(tool_calls=[_signal_call(), unknown])])
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(
            message="есть ли утюг и вызовите такси", provider=provider
        )

    assert turn.kind is TurnKind.NEEDS_HUMAN
    assert turn.escalation is not None
    assert turn.reply_text == orchestrator._ESCALATION_TEXT
    assert turn.unanswered_question == "есть ли в номере утюг"


async def test_escalation_by_failed_execution_keeps_the_question_too(
    demo_tenant: uuid.UUID,
) -> None:
    """Второй путь эскалации — упавшее исполнение (ERR-AI-004): та же развилка,
    но она живёт в другой ветке кода, поэтому проверяется отдельно."""
    bad_category = ToolCall(
        id="toolu_bad",
        name="create_service_request",
        arguments={
            "category_key": "spa",  # вне enum тенанта — ERR-AI-004 на исполнении
            "summary": "массаж",
            "room_number": "305",
            "confirmation_question": "Оформить?",
            "guest_language": "ru",
            "is_urgent": True,  # гейт снят — доходим до исполнения на этом же ходу
        },
    )
    provider = ScriptedLlmProvider([MockTurn(tool_calls=[_signal_call(), bad_category])])
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(
            message="есть ли утюг и запишите на массаж", provider=provider
        )
        assert await _request_total() == 0

    assert turn.kind is TurnKind.NEEDS_HUMAN
    assert turn.reply_text == orchestrator._ESCALATION_TEXT
    assert turn.unanswered_question == "есть ли в номере утюг"


async def test_turn_without_signal_is_byte_for_byte_the_old_one(demo_tenant: uuid.UUID) -> None:
    """Ход без сигнала ведёт себя ровно как до этого PR: поле исхода пустое,
    реплика — прежняя (защита от «сигнал подмешался в обычный ход»)."""
    provider = ScriptedLlmProvider([MockTurn(text="Оформить заявку?", tool_calls=[_water_call()])])
    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(message="принесите воду в 305", provider=provider)

    assert turn.kind is TurnKind.AWAITING_CONFIRMATION
    assert turn.unanswered_question is None
    assert turn.reply_text == CONFIRMATION
