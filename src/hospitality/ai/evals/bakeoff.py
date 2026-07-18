"""Bake-off моделей гостевого диалога на 6 языках пилота (Task 0015, §7.7, ADR-010).

Прогоняет одинаковые сценарии через оркестратор на РЕАЛЬНЫХ Haiku 4.5 и Sonnet 5
(через единственную дверь — gateway, §7.2) и печатает исходы для оценки. Не CI:
стоит токены, недетерминирован, исключён из покрытия. Итог фиксирует `LLM_MODEL`.

Что смотрим (ADR-010): корректность выбора инструмента/категории на запросах;
ОТКАЗ от галлюцинации цен/правил (RAG нет — Phase 0: модель обязана эскалировать,
а не выдумывать, §7.4); поведение на 6 языках, приоритет — казахский (kk).

Запуск (нужен ANTHROPIC_API_KEY в .env и поднятый Postgres — тот же, что у app):

    python -m hospitality.ai.evals.bakeoff
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select

from hospitality.ai import orchestrator
from hospitality.ai.gateway.api import LlmMessage, LlmProvider, build_anthropic_provider
from hospitality.modules.requests import api as requests_api
from hospitality.modules.requests.api import (
    ERR_REQUESTS_CATEGORY_KEY_TAKEN,
    RequestCategoryCreate,
    create_category,
)
from hospitality.platform.models import Tenant
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

CANDIDATE_MODELS = ["claude-haiku-4-5", "claude-sonnet-5"]

# Категории eval-тенанта — чтобы у выбора инструмента был реальный набор.
_EVAL_CATEGORIES = [
    ("housekeeping", "Housekeeping"),
    ("engineering", "Engineering"),
    ("room-service", "Room service"),
    ("it", "IT"),
]

Kind = Literal["request", "high_stakes", "escalation"]


@dataclass(frozen=True)
class Scenario:
    language: str
    kind: Kind
    message: str
    # Что считаем правильным исходом (для ручной/LLM-оценки, не автоматической).
    expectation: str


# 6 языков пилота (ADR-010). request — ждём вызов инструмента с верной категорией;
# high_stakes — ждём ОТКАЗ выдумывать (эскалация/уточнение у сотрудника);
# escalation — ждём передачу человеку.
SCENARIOS: list[Scenario] = [
    Scenario("en", "request", "Please clean my room, 305.", "tool: housekeeping"),
    Scenario(
        "en", "high_stakes", "How much does a late checkout cost?", "no price invented → escalate"
    ),
    Scenario("en", "escalation", "I want to speak to a staff member.", "hand off to human"),
    Scenario("ru", "request", "Уберите номер 305, пожалуйста.", "tool: housekeeping"),
    Scenario("ru", "high_stakes", "Сколько стоит поздний выезд?", "no price invented → escalate"),
    Scenario(
        "kk", "request", "305 бөлмені тазалап беріңізші.", "tool: housekeeping (низкоресурсный kk!)"
    ),
    Scenario("kk", "high_stakes", "Кеш шығу қанша тұрады?", "no price invented → escalate (kk!)"),
    Scenario("kk", "escalation", "Маған қызметкермен сөйлескім келеді.", "hand off to human (kk!)"),
    Scenario("zh", "request", "请打扫一下305房间。", "tool: housekeeping"),
    Scenario("zh", "high_stakes", "延迟退房要多少钱？", "no price invented → escalate"),
    Scenario("tr", "request", "Lütfen 305 numaralı odayı temizleyin.", "tool: housekeeping"),
    Scenario("tr", "high_stakes", "Geç çıkış ücreti ne kadar?", "no price invented → escalate"),
    Scenario("hi", "request", "कृपया कमरा 305 साफ़ कर दीजिए।", "tool: housekeeping"),
    Scenario("hi", "high_stakes", "लेट चेकआउट का कितना चार्ज है?", "no price invented → escalate"),
]


# Подтверждение гостя («да») на языке сценария — второй ход гейта P-9. Ход
# подтверждения детерминирован (forced_tool классификатора, Task 0017.1), но
# первый ход (вооружение гейта вызовом инструмента) зависит от модели — именно он
# ломался в баге #71 (v2-промпт учил Haiku придерживать tool_use). Ассерт ниже
# проходит весь путь до строки в БД, чтобы регрессия ловилась на реальной модели.
CONFIRM_BY_LANGUAGE: dict[str, str] = {
    "en": "Yes, please go ahead.",
    "ru": "Да, оформляйте, пожалуйста.",
    "kk": "Иә, өтінемін, рәсімдеңіз.",
    "zh": "好的，麻烦你了。",
    "tr": "Evet, lütfen oluşturun.",
    "hi": "हाँ, कृपया कर दीजिए।",
}


async def _assert_request_created(
    provider: LlmProvider, tenant_id: uuid.UUID, scenario: Scenario
) -> tuple[bool, str]:
    """Пройти весь путь заявки на реальной модели: предложение → «да» → строка в БД.

    Возвращает `(created, detail)`. `created=False` — регрессия #71 (гейт не
    вооружился на первом ходу или заявка не создалась после подтверждения).
    Первый ход недетерминирован (модель может не вызвать инструмент); ассерт
    именно это и стережёт — прогон перед деплоем промпта/модели (§7.7).
    """
    with tenant_context(tenant_id):
        before = (await requests_api.list_requests(limit=1, offset=0)).total
        proposal = await orchestrator.handle_message(message=scenario.message, provider=provider)
    if proposal.pending_action is None:
        return False, (
            f"гейт P-9 НЕ вооружён на первом ходу (kind={proposal.kind.value}, "
            f"инструмент не вызван) — заявку создать нечем: {proposal.reply_text[:70]!r}"
        )

    confirm = CONFIRM_BY_LANGUAGE[scenario.language]
    with tenant_context(tenant_id):
        done = await orchestrator.handle_message(
            message=confirm,
            history=[
                LlmMessage(role="user", content=scenario.message),
                LlmMessage(role="assistant", content=proposal.reply_text),
            ],
            pending_action=proposal.pending_action,
            provider=provider,
        )
        after = (await requests_api.list_requests(limit=1, offset=0)).total

    if done.created_request_id is None or after != before + 1:
        return False, (
            f"после «{confirm}» заявка НЕ создана (kind={done.kind.value}, "
            f"created_request_id={done.created_request_id}, total {before}→{after})"
        )
    return True, f"заявка {done.created_request_id} создана (total {before}→{after})"


async def _ensure_eval_tenant() -> None:
    """Создать eval-тенанта с категориями (идемпотентно)."""
    async with platform_session_scope() as session:
        existing = await session.scalar(select(Tenant).where(Tenant.slug == "bakeoff-eval"))
        if existing is None:
            session.add(Tenant(slug="bakeoff-eval", name="Bake-off Eval"))
            await session.flush()
    tenant_id = await _eval_tenant_id()
    with tenant_context(tenant_id):
        for key, name in _EVAL_CATEGORIES:
            try:
                await create_category(RequestCategoryCreate(key=key, name=name))
            except AppError as error:
                if error.code != ERR_REQUESTS_CATEGORY_KEY_TAKEN:
                    raise


async def _eval_tenant_id() -> uuid.UUID:
    async with platform_session_scope() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == "bakeoff-eval"))
        assert tenant is not None
        return tenant.id


def _summarize(turn: orchestrator.OrchestratorTurn) -> str:
    if turn.pending_action is not None:
        args = turn.pending_action.arguments
        return (
            f"TOOL[{turn.kind.value}] {turn.pending_action.tool_name}({args.get('category_key')!r})"
        )
    return f"{turn.kind.value}: {turn.reply_text[:80]!r}"


async def run() -> None:
    settings = get_settings()
    if not settings.anthropic_api_key:
        print("ANTHROPIC_API_KEY не задан — bake-off требует реального ключа (.env).")
        return

    await _ensure_eval_tenant()
    tenant_id = await _eval_tenant_id()

    print("Bake-off: Haiku 4.5 vs Sonnet 5 (§7.7, ADR-010).")
    print("Цена: Haiku $1/$5, Sonnet $3/$15 за Mtok. Оценка исходов — ручная/LLM-judge.")
    print(f"Активная модель рантайма (LLM_MODEL): {settings.llm_model}\n")

    # Регрессия #71 ловится ассертом только на активной модели рантайма: именно
    # её промпт+модель должны надёжно создавать заявку. Остальные кандидаты —
    # информационное сравнение (print), без жёсткого гейта.
    request_failures: list[str] = []

    for model in CANDIDATE_MODELS:
        provider = build_anthropic_provider(model)
        print(f"\n===== {model} =====")
        for scenario in SCENARIOS:
            try:
                with tenant_context(tenant_id):
                    turn = await orchestrator.handle_message(
                        message=scenario.message, provider=provider
                    )
                got = _summarize(turn)
            except AppError as error:
                # Одна упавшая реплика (отказ модели/ошибка API) не рушит прогон.
                got = f"ERROR {error.code}: {error.message}"
            print(
                f"[{scenario.language}/{scenario.kind}] want: {scenario.expectation}\n"
                f"    msg: {scenario.message}\n"
                f"    got: {got}"
            )

        # Сквозной ассерт создания заявки (#71): проходим весь путь до строки в БД
        # для каждого request-сценария. Печатаем исход всегда; жёстко валит прогон
        # только активная модель рантайма (её и деплоим).
        print(f"  --- сквозной ассерт заявки (два хода → строка в БД), {model} ---")
        for scenario in SCENARIOS:
            if scenario.kind != "request":
                continue
            try:
                created, detail = await _assert_request_created(provider, tenant_id, scenario)
            except AppError as error:
                created, detail = False, f"ERROR {error.code}: {error.message}"
            mark = "OK " if created else "!! "
            print(f"  {mark}[{scenario.language}/request] {detail}")
            if not created and model == settings.llm_model:
                request_failures.append(f"{model} [{scenario.language}]: {detail}")

    if request_failures:
        print("\nПРОВАЛ сквозного ассерта заявки на активной модели (баг #71):")
        for failure in request_failures:
            print(f"  - {failure}")
        raise AssertionError(
            f"{len(request_failures)} request-сценарий(ев) активной модели "
            f"{settings.llm_model} не создали заявку — гейт P-9 не сработал (#71)"
        )
    print("\nСквозной ассерт заявки на активной модели пройден: все языки создали заявку.")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except AssertionError as error:
        print(f"\nBAKE-OFF FAILED: {error}")
        sys.exit(1)
