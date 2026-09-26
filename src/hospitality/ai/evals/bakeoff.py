"""Bake-off моделей гостевого диалога на 6 языках пилота (Task 0015, §7.7, ADR-010).

Прогоняет одинаковые сценарии через оркестратор на РЕАЛЬНЫХ Haiku 4.5 и Sonnet 5
(через единственную дверь — gateway, §7.2) и печатает исходы для оценки. Не CI:
стоит токены, недетерминирован, исключён из покрытия. Итог фиксирует `LLM_MODEL`.

Что смотрим (ADR-010): корректность выбора инструмента/категории на запросах;
ОТКАЗ от галлюцинации цен/правил (RAG нет — §7.4); поведение на 6 языках,
приоритет — казахский (kk).

Что именно значит «отказ», с spec 0036 (issue #334) изменилось, и это не
косметика: до неё промпт велел на вопрос о ценах, правилах и часах «честно
сказать, что уточню у сотрудника, и предложить его привести», то есть отказ
выглядел эскалацией. Теперь цены, правила и часы — предмет справочника отеля, и
вопрос, не покрытый фактом, обязан дать честное «этого у меня нет» + указание на
ресепшен + вызов служебного сигнала `report_unanswered_question`, БЕЗ обещания
сотрудника (§6 спеки: обещать некому — #101 открыт, а после #101 обещание
сделало бы эскалацией каждый неизвестный факт). Сотрудник остаётся за «нужен
человек сейчас» — это сценарии `escalation`, и они не менялись.

Запуск (нужен ANTHROPIC_API_KEY в .env и поднятый Postgres — тот же, что у app):

    python -m hospitality.ai.evals.bakeoff
    ENABLE_SERVICE_REQUESTS=true python -m hospitality.ai.evals.bakeoff
    python -m hospitality.ai.evals.bakeoff --model claude-sonnet-5 --repeat 6

Режим `ENABLE_SERVICE_REQUESTS` берётся из окружения, и первая строка выдачи его
называет: проверки двух режимов разные, а прогон в чужом режиме печатает
провалы по построению. Умолчание — «только консультации» (PR #349, боевой режим
пилота): заявок нет, `request` и `*_mixed` ждут отказа словами и НЕ ждут
сигнала на просьбе (`_check_consultation_turn`), сквозной ассерт заявки не
гоняется — его стережёт режим `true` и job `smoke` в CI. Прогон из DoD —
в обоих режимах. `--repeat N` повторяет каждый сценарий и печатает итог
«провалов из N» по сценариям — редкий отказ одним прогоном не виден.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass
from datetime import timedelta
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
from hospitality.platform.config import TenantConfig, store_tenant_config
from hospitality.platform.models import Tenant
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
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

Kind = Literal[
    "request",
    "high_stakes",
    "escalation",
    "hotel_fact",
    "hotel_fact_mixed",
    "hotel_fact_unknown",
    "hotel_fact_unknown_mixed",
    "hotel_fact_partial",
    "hotel_fact_unknown_two",
]

# Справочник eval-тенанта (spec 0036 §4): те же три факта, что в примере спеки.
# Срок временного факта отсчитывается от дня прогона — иначе он протухнет, и
# сценарий «временный факт» начнёт проверять «просроченное не рендерится».
# Тем «поздний выезд» здесь НЕТ намеренно: сценарии high_stakes выше проверяют
# отказ выдумывать цену, и факт про неё отменил бы их предмет.
_POOL_CLOSED_UNTIL = (utc_now() + timedelta(days=30)).date()
_EVAL_HOTEL_FACTS = [
    {"topic": "Завтрак", "answer": "с 07:00 до 10:30 на 2 этаже, входит в тариф"},
    {"topic": "Wi-Fi", "answer": "сеть Grand-Guest, пароль welcome2026"},
    {
        "topic": "Бассейн",
        "answer": "закрыт на ремонт",
        "valid_until": _POOL_CLOSED_UNTIL.isoformat(),
    },
]


@dataclass(frozen=True)
class Scenario:
    language: str
    kind: Kind
    message: str
    # Что считаем правильным исходом (для ручной/LLM-оценки, не автоматической).
    expectation: str
    # Машинные проверки ходов с несколькими вопросами (ревью PR #344): значение
    # факта, обязанное дойти до ГОСТЯ; корни тем, обязанные дойти до МЕНЕДЖЕРА
    # строкой справочника; корни тем, покрытых фактом, которым в этой строке не
    # место (сравнение без учёта регистра).
    fact_value: str | None = None
    question_topics: tuple[str, ...] = ()
    covered_topics: tuple[str, ...] = ()


# 6 языков пилота (ADR-010). request — ждём вызов инструмента с верной категорией;
# high_stakes — ждём ОТКАЗ выдумывать цену: «этого у меня нет» + ресепшен + вызов
# `report_unanswered_question` (spec 0036 §6, issue #337). Своего вида в `Kind`
# эти сценарии не получают намеренно: предмет у них прежний — не выдумать цену, —
# изменился только вид правильного отказа, а «вопрос без ответа» как отдельный
# предмет уже проверяют `hotel_fact_unknown*` ниже, на справочнике из трёх фактов.
# escalation — ждём передачу человеку (не менялось: «нужен человек сейчас»).
SCENARIOS: list[Scenario] = [
    Scenario("en", "request", "Please clean my room, 305.", "tool: housekeeping"),
    Scenario(
        "en",
        "high_stakes",
        "How much does a late checkout cost?",
        "цену не выдумал → ресепшен + сигнал",
    ),
    Scenario("en", "escalation", "I want to speak to a staff member.", "hand off to human"),
    Scenario("ru", "request", "Уберите номер 305, пожалуйста.", "tool: housekeeping"),
    Scenario(
        "ru", "high_stakes", "Сколько стоит поздний выезд?", "цену не выдумал → ресепшен + сигнал"
    ),
    Scenario(
        "kk", "request", "305 бөлмені тазалап беріңізші.", "tool: housekeeping (низкоресурсный kk!)"
    ),
    Scenario(
        "kk", "high_stakes", "Кеш шығу қанша тұрады?", "цену не выдумал → ресепшен + сигнал (kk!)"
    ),
    Scenario("kk", "escalation", "Маған қызметкермен сөйлескім келеді.", "hand off to human (kk!)"),
    Scenario("zh", "request", "请打扫一下305房间。", "tool: housekeeping"),
    Scenario("zh", "high_stakes", "延迟退房要多少钱？", "цену не выдумал → ресепшен + сигнал"),
    Scenario("tr", "request", "Lütfen 305 numaralı odayı temizleyin.", "tool: housekeeping"),
    Scenario(
        "tr", "high_stakes", "Geç çıkış ücreti ne kadar?", "цену не выдумал → ресепшен + сигнал"
    ),
    Scenario("hi", "request", "कृपया कमरा 305 साफ़ कर दीजिए।", "tool: housekeeping"),
    Scenario(
        "hi", "high_stakes", "लेट चेकआउट का कितना चार्ज है?", "цену не выдумал → ресепшен + сигнал"
    ),
]

# Знания об отеле (spec 0036 §5, issue #333/#334) — языки пилота ru/kk/en.
# Восемь сценариев: четыре пришли с #333, два — с веткой «факта нет» и
# инструментом report_unanswered_question (#334), до него недостижимые, и два —
# с ревью PR #344, которое нашло на них потерю ответа, невидимую юнит-тестам.
# hotel_fact — ждём ответ ИЗ факта, дословными числами и без выдумки;
# hotel_fact_mixed — ждём вызов инструмента, у которого ответ на вопрос стоит
# В НАЧАЛЕ confirmation_question (свободный текст на таком ходу гость не видит);
# на обоих вызов сигнала «в справочнике нет ответа» — провал: вопрос покрыт;
# hotel_fact_unknown — ждём честное «нет в справке» + сигнал, БЕЗ обещания
# сотрудника (§6: обещать некому, а после #101 это сделало бы эскалацией каждый
# неизвестный факт);
# hotel_fact_unknown_mixed — ждём и сигнал, и заявку: реплика сигнала первым
# абзацем, confirmation_question остаётся ЧИСТЫМ вопросом-подтверждением;
# hotel_fact_partial — вопрос покрыт + вопрос не покрыт, действия нет: значение
# факта обязано дойти до гостя (гость видит только `reply_to_guest`, и без
# правила описаний модель клала туда лишь «про утюг не знаю» — 17 из 20), а
# покрытая тема — НЕ попасть в строку менеджеру;
# hotel_fact_unknown_two — два вопроса без факта: оба обязаны дойти до
# менеджера одной строкой справочника.
HOTEL_FACT_SCENARIOS: list[Scenario] = [
    Scenario("ru", "hotel_fact", "Во сколько завтрак?", "из факта: 07:00–10:30, 2 этаж"),
    Scenario("kk", "hotel_fact", "Таңғы ас нешеде?", "из факта: 07:00–10:30 (kk!)"),
    Scenario("en", "hotel_fact", "What time is breakfast?", "из факта: 07:00–10:30, 2nd floor"),
    Scenario("ru", "hotel_fact", "Бассейн работает?", "ВРЕМЕННО закрыт, с датой"),
    Scenario("kk", "hotel_fact", "Бассейн жұмыс істей ме?", "ВРЕМЕННО закрыт, с датой (kk!)"),
    Scenario("en", "hotel_fact", "Is the pool open?", "ВРЕМЕННО закрыт, с датой"),
    Scenario("ru", "hotel_fact", "Какой пароль от вайфая?", "welcome2026 ДОСЛОВНО"),
    Scenario("kk", "hotel_fact", "Wi-Fi құпия сөзі қандай?", "welcome2026 ДОСЛОВНО (kk!)"),
    Scenario("en", "hotel_fact", "What is the Wi-Fi password?", "welcome2026 ДОСЛОВНО"),
    Scenario(
        "ru",
        "hotel_fact_mixed",
        "Во сколько завтрак и принесите полотенца в 305",
        "tool + ответ про завтрак В НАЧАЛЕ confirmation_question",
        fact_value="07:00",
    ),
    Scenario(
        "kk",
        "hotel_fact_mixed",
        "Таңғы ас нешеде және 305-ке сүлгі әкеліңізші",
        "tool + ответ про завтрак В НАЧАЛЕ confirmation_question (kk!)",
        fact_value="07:00",
    ),
    Scenario(
        "en",
        "hotel_fact_mixed",
        "What time is breakfast, and please bring towels to 305",
        "tool + ответ про завтрак В НАЧАЛЕ confirmation_question",
        fact_value="07:00",
    ),
    # Вопрос, которого в справочнике нет (утюга среди трёх фактов нет вовсе).
    Scenario(
        "ru", "hotel_fact_unknown", "Есть ли в номере утюг?", "нет в справке + ресепшен + сигнал"
    ),
    Scenario(
        "kk",
        "hotel_fact_unknown",
        "Бөлмеде үтік бар ма?",
        "нет в справке + ресепшен + сигнал (kk!)",
    ),
    Scenario(
        "en",
        "hotel_fact_unknown",
        "Is there an iron in the room?",
        "нет в справке + ресепшен + сигнал",
    ),
    Scenario(
        "ru",
        "hotel_fact_unknown_mixed",
        "Есть ли утюг и принесите, пожалуйста, воду в 305",
        "сигнал + заявка: реплика сигнала первым абзацем, вопрос-подтверждение чистый",
    ),
    Scenario(
        "kk",
        "hotel_fact_unknown_mixed",
        "Үтік бар ма және 305-ке су әкеліңізші",
        "сигнал + заявка: реплика сигнала первым абзацем (kk!)",
    ),
    Scenario(
        "en",
        "hotel_fact_unknown_mixed",
        "Is there an iron, and please bring water to 305",
        "сигнал + заявка: реплика сигнала первым абзацем, вопрос-подтверждение чистый",
    ),
    Scenario(
        "ru",
        "hotel_fact_partial",
        "Во сколько завтрак и есть ли у вас парковка?",
        "ответ про завтрак ДОШЁЛ + про парковку нет в справке + сигнал",
        fact_value="07:00",
        covered_topics=("завтрак",),
    ),
    Scenario(
        "kk",
        "hotel_fact_partial",
        "Wi-Fi құпия сөзі қандай және бөлмеде үтік бар ма?",
        "пароль ДОШЁЛ + про утюг нет в справке + сигнал (kk!)",
        fact_value="welcome2026",
        covered_topics=("wi-fi", "wifi", "құпия"),
    ),
    Scenario(
        "en",
        "hotel_fact_partial",
        "What's the Wi-Fi password, and is there an iron in the room?",
        "пароль ДОШЁЛ + про утюг нет в справке + сигнал",
        fact_value="welcome2026",
        covered_topics=("wi-fi", "wifi", "password"),
    ),
    Scenario(
        "ru",
        "hotel_fact_unknown_two",
        "Есть ли у вас прачечная? И где ближайшая аптека?",
        "оба вопроса — строкой менеджеру, гостю — про оба",
        question_topics=("прачечн", "аптек"),
    ),
    Scenario(
        "kk",
        "hotel_fact_unknown_two",
        "Кір жуатын орын бар ма? Ең жақын дәріхана қайда?",
        "оба вопроса — строкой менеджеру, гостю — про оба (kk!)",
        question_topics=("кір", "дәріхана"),
    ),
    Scenario(
        "en",
        "hotel_fact_unknown_two",
        "Is there a laundry? And where is the nearest pharmacy?",
        "оба вопроса — строкой менеджеру, гостю — про оба",
        question_topics=("laundr", "pharmac"),
    ),
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

    # spec 0021 П-1: модель обязана назвать язык гостя — на нём уйдут статусные
    # уведомления. Сверяем с языком сценария (мягкая нормализация как в инструменте).
    raw_language = str(proposal.pending_action.arguments.get("guest_language") or "")
    if raw_language.strip().lower()[:2] != scenario.language:
        return False, (
            f"guest_language={raw_language!r} не совпал с языком гостя ({scenario.language}) "
            "— статусные уведомления уйдут не на том языке (spec 0021 П-1)"
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
    """Создать eval-тенанта с категориями и справочником отеля (идемпотентно)."""
    async with platform_session_scope() as session:
        existing = await session.scalar(select(Tenant).where(Tenant.slug == "bakeoff-eval"))
        if existing is None:
            session.add(Tenant(slug="bakeoff-eval", name="Bake-off Eval"))
            await session.flush()
    tenant_id = await _eval_tenant_id()
    # Конфиг перезаписывается каждым прогоном: срок временного факта считается
    # от сегодняшнего дня, и вчерашний конфиг проверял бы не тот сценарий.
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig.model_validate(
                {
                    "schema_version": 1,
                    "profile": {"city": "Almaty", "country_code": "KZ"},
                    "timezone": "Asia/Almaty",
                    "default_language": "ru",
                    "hotel_facts": _EVAL_HOTEL_FACTS,
                }
            ),
        )
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


def _check_hotel_fact_turn(
    scenario: Scenario, turn: orchestrator.OrchestratorTurn
) -> tuple[bool, str]:
    """Оценить ход со справочником отеля машинно там, где это возможно (spec 0036).

    Дословность и «названо временным» оценивает человек по напечатанному
    ответу — автоматически это не проверить. Машинно проверяется то, чего не
    проверит юнит-тест: аргументы модели он подставляет сам.
    """
    if scenario.kind == "hotel_fact_unknown":
        # Вопрос без факта (spec 0036 §6): ход остаётся REPLY, вопрос записан,
        # реплика — из аргумента сигнала. Сотрудника ветка обещать не должна —
        # это оценивает человек по напечатанному (машинно «обещание» не поймать).
        if turn.pending_action is not None:
            return False, f"вместо ответа вызван инструмент {turn.pending_action.tool_name}"
        if turn.unanswered_question is None:
            return False, (
                "сигнал report_unanswered_question НЕ вызван — вопрос не попадёт "
                f"менеджеру: {turn.reply_text[:90]!r}"
            )
        ok, detail = _check_language(scenario, turn.reply_text)
        return ok, f"вопрос записан: {turn.unanswered_question!r}; реплика: {detail}"

    if scenario.kind == "hotel_fact_unknown_mixed":
        return _check_unknown_mixed_turn(scenario, turn)
    if scenario.kind in ("hotel_fact_partial", "hotel_fact_unknown_two"):
        return _check_several_questions_turn(scenario, turn)
    return _check_covered_question_turn(scenario, turn)


def _check_covered_question_turn(
    scenario: Scenario, turn: orchestrator.OrchestratorTurn
) -> tuple[bool, str]:
    """Вопрос покрыт фактом целиком: `hotel_fact` и `hotel_fact_mixed` (spec 0036 §5).

    Первым проверяется ЛОЖНЫЙ сигнал (ревью PR #344, находка 11). Описание
    сигнала называет покрытые фактом части сообщения прямо — «your whole answer
    goes there — including every part … that a hotel fact does answer», — и
    естественный отказ такой схемы — звать сигнал и на вопросе, который факт
    закрывает. Менеджер получил бы строку «Во сколько завтрак?» при заполненном
    факте, а по реплике этого не видно: пометку `+сигнал` ставит только
    `_summarize` сценариев без справочника.
    """
    if turn.unanswered_question is not None:
        return False, (
            "ЛОЖНЫЙ сигнал на вопросе, покрытом фактом — менеджер получит строку "
            f"{turn.unanswered_question!r}; реплика: {turn.reply_text[:90]!r}"
        )
    if scenario.kind == "hotel_fact_mixed":
        return _check_covered_mixed_turn(scenario, turn)
    # Простой вопрос: инструмента быть не должно, ответ — обычной репликой.
    if turn.pending_action is not None:
        return False, f"вместо ответа вызван инструмент {turn.pending_action.tool_name}"
    return _check_language(scenario, turn.reply_text)


def _check_covered_mixed_turn(
    scenario: Scenario, turn: orchestrator.OrchestratorTurn
) -> tuple[bool, str]:
    """Вопрос покрыт фактом + просьба (spec 0036 §6, «Смешанный ход»).

    Ответ обязан лежать В НАЧАЛЕ `confirmation_question` инструмента, а не в
    свободном тексте — свободный текст гейт P-9 гостю не показывает вовсе, и без
    правила промпта v5 ответ про завтрак пропал бы молча.
    """
    if turn.pending_action is None:
        return False, (
            f"заявка НЕ предложена (kind={turn.kind.value}) — просьба потерялась: "
            f"{turn.reply_text[:70]!r}"
        )
    question = str(turn.pending_action.arguments.get("confirmation_question") or "")
    # «07:00» — общая часть ответа про завтрак на всех трёх языках.
    if "07:00" not in question:
        return False, (
            f"ответ про завтрак НЕ попал в confirmation_question={question[:70]!r} "
            f"(свободный текст модели: {turn.reply_text[:70]!r}) — гость его не увидит"
        )
    # `find` отдаёт -1, когда «?» в строке нет вовсе, и сравнение с -1 объявляло
    # бы провалом верный ход: вопрос-подтверждение модель нередко пишет без
    # знака вопроса («Оформлю заявку на полотенца в номер 305»). Нет «?» —
    # границу вопроса машинно не найти, и проверка порядка молчит (порядок в
    # таком ответе оценивает человек по напечатанному, как дословность выше);
    # ложное «провал» стоило бы дороже: по нему пошли бы чинить исправный промпт.
    question_mark = question.find("?")
    if question.index("07:00") > (question_mark if question_mark != -1 else len(question)):
        return False, f"ответ стоит ПОСЛЕ вопроса-подтверждения: {question[:100]!r}"
    return _check_language(scenario, question)


def _check_unknown_mixed_turn(
    scenario: Scenario, turn: orchestrator.OrchestratorTurn
) -> tuple[bool, str]:
    """Смешанный ход БЕЗ факта (spec 0036 §6, DoD issue #334).

    Проверяется машинно ровно то, чего не проверит юнит-тест (он подставляет
    аргументы модели сам): сигнал вызван, заявка не потеряна из-за него, а
    реплика гостю начинается с реплики сигнала и продолжается
    вопросом-подтверждением. «`confirmation_question` остался ЧИСТЫМ
    вопросом-подтверждением» — предмет человеческой оценки: перефразированный
    повтор ответа машинно от нового текста не отличить, поэтому обе строки
    печатаются целиком.
    """
    if turn.unanswered_question is None:
        return False, (
            "сигнал report_unanswered_question НЕ вызван — вопрос не попадёт менеджеру "
            f"(kind={turn.kind.value}): {turn.reply_text[:90]!r}"
        )
    if turn.pending_action is None:
        return False, (
            f"заявка НЕ предложена (kind={turn.kind.value}) — просьба потерялась под "
            f"сигналом: {turn.reply_text[:90]!r}"
        )
    question = str(turn.pending_action.arguments.get("confirmation_question") or "")
    if "\n\n" not in turn.reply_text:
        return False, (
            "реплика сигнала НЕ склеена с вопросом-подтверждением — гость получит "
            f"только одну часть: {turn.reply_text[:120]!r}"
        )
    signal_reply, _, tail = turn.reply_text.partition("\n\n")
    if tail.strip() != question.strip():
        return False, (
            f"вторым абзацем стоит не вопрос-подтверждение: {tail[:90]!r} "
            f"vs confirmation_question={question[:90]!r}"
        )
    ok, detail = _check_language(scenario, turn.reply_text)
    if not ok:
        return ok, detail
    return True, (
        f"вопрос записан: {turn.unanswered_question!r}\n"
        f"      1-й абзац (сигнал): {signal_reply!r}\n"
        f"      2-й абзац (гейт P-9, обязан быть ЧИСТЫМ вопросом): {question!r}"
    )


def _check_several_questions_turn(
    scenario: Scenario, turn: orchestrator.OrchestratorTurn
) -> tuple[bool, str]:
    """Несколько вопросов на ходу без действия (ревью PR #344).

    Машинно проверяется ровно то, что ревью нашло потерянным на живой модели и
    чего не видят юнит-тесты (аргументы они подставляют сами): значение факта
    дошло до ГОСТЯ, а не осталось в свободном тексте, которого он не видит; все
    вопросы без факта дошли до МЕНЕДЖЕРА строкой справочника, а покрытое фактом
    в эту строку не попало — схема велит «Leave out anything a fact does answer».
    """
    if turn.pending_action is not None:
        return False, f"вместо ответа вызван инструмент {turn.pending_action.tool_name}"
    row = turn.unanswered_question
    if row is None:
        return False, (
            f"сигнал report_unanswered_question НЕ вызван — вопрос не попадёт менеджеру: "
            f"{turn.reply_text[:90]!r}"
        )
    if scenario.fact_value is not None and scenario.fact_value not in turn.reply_text:
        return False, (
            f"значение факта {scenario.fact_value!r} НЕ дошло до гостя: {turn.reply_text[:120]!r}"
        )
    missing = [topic for topic in scenario.question_topics if topic not in row.lower()]
    if missing:
        return False, f"до менеджера не дошли темы {missing}: строка {row!r}"
    leaked = [topic for topic in scenario.covered_topics if topic in row.lower()]
    if leaked:
        return False, f"в строку менеджеру попала тема, покрытая фактом, {leaked}: {row!r}"
    ok, detail = _check_language(scenario, turn.reply_text)
    return ok, f"вопрос записан: {row!r}; реплика: {detail}"


def _check_language(scenario: Scenario, reply: str) -> tuple[bool, str]:
    """Ответ на языке ГОСТЯ, а не на языке факта (spec 0036 §5, правило v5).

    Справочник написан по-русски, и это перетягивает язык ответа: прогон
    07.09.2026 поймал Sonnet 5, отвечающего английскому гостю по-русски на трёх
    сценариях из четырёх. Машинно ловится только пара «латиница vs кириллица» —
    ru и kk обе кириллические, их различает человек по напечатанному ответу.
    """
    if scenario.language == "en" and any("а" <= char.lower() <= "я" for char in reply):
        return False, f"ответ гостю-англичанину ушёл кириллицей: {reply[:90]!r}"
    return True, reply


# Сценарии, чья проверка в режиме консультаций своя: заявки нет по построению.
# Значение — что печатать в «want» вместо ожидания режима `true`.
_CONSULTATION_OWN_CHECK: dict[str, str] = {
    "request": "отказ словами «оформить не могу → ресепшен», сигнала НЕТ",
    "high_stakes": "цену не выдумал → ресепшен + сигнал",
    "escalation": "ресепшен, без обещания позвать сотрудника (читать глазами)",
    "hotel_fact_mixed": "ответ из факта + отказ от просьбы словами, ни инструмента, ни сигнала",
    "hotel_fact_unknown_mixed": "нет в справке + отказ от просьбы словами + сигнал",
}


def _expectation(scenario: Scenario, consultation: bool) -> str:
    """Ожидание сценария в режиме прогона — для печати рядом с исходом."""
    if consultation and scenario.kind in _CONSULTATION_OWN_CHECK:
        return f"{_CONSULTATION_OWN_CHECK[scenario.kind]} [консультации]"
    return scenario.expectation


def _check_consultation_turn(
    scenario: Scenario, turn: orchestrator.OrchestratorTurn
) -> tuple[bool, str]:
    """Ход в режиме «только консультации» (ревью PR #344, находки 14–17).

    Инструмента создания заявки в запросе нет, поэтому `request` и оба
    `*_mixed` ждут не заявку, а отказ словами («оформить не могу — ресепшен»:
    это оценивает человек по напечатанному). Машинно — то, что ревью нашло на
    этом режиме: сигнал на ПРОСЬБЕ (строка «Guest in room 305 requests room
    cleaning» у менеджера), значение факта, не дошедшее до гостя, и ответ
    англичанину кириллицей. Сценарии справочника без просьбы проверяются так
    же, как в режиме `true`.
    """
    if scenario.kind not in _CONSULTATION_OWN_CHECK:
        return _check_hotel_fact_turn(scenario, turn)
    signal = turn.unanswered_question
    if turn.pending_action is not None:
        return False, f"в режиме консультаций предложено действие {turn.pending_action.tool_name}"
    if scenario.kind in ("request", "hotel_fact_mixed") and signal is not None:
        return False, (
            f"сигнал на вопросе, который факт покрывает, или на ПРОСЬБЕ — менеджер "
            f"получит строку {signal!r}; реплика: {turn.reply_text[:90]!r}"
        )
    if scenario.kind in ("high_stakes", "hotel_fact_unknown_mixed") and signal is None:
        return False, (
            f"сигнал report_unanswered_question НЕ вызван — вопрос не попадёт менеджеру: "
            f"{turn.reply_text[:90]!r}"
        )
    if scenario.fact_value is not None and scenario.fact_value not in turn.reply_text:
        return False, (
            f"значение факта {scenario.fact_value!r} НЕ дошло до гостя: {turn.reply_text[:120]!r}"
        )
    ok, detail = _check_language(scenario, turn.reply_text)
    return ok, detail if signal is None else f"вопрос записан: {signal!r}; реплика: {detail}"


def _summarize(turn: orchestrator.OrchestratorTurn) -> str:
    """Однострочный исход хода для ручной оценки.

    Сигнал «в справочнике нет ответа» показывается явно (issue #337): после
    spec 0036 правильный отказ высокоставочного сценария — «этого у меня нет» +
    ресепшен + сигнал, и без пометки `+сигнал` человек не отличит его от того же
    текста БЕЗ вызова, то есть от вопроса, который не доедет до менеджера.
    """
    signal = "" if turn.unanswered_question is None else f" +сигнал({turn.unanswered_question!r})"
    if turn.pending_action is not None:
        args = turn.pending_action.arguments
        return (
            f"TOOL[{turn.kind.value}] "
            f"{turn.pending_action.tool_name}({args.get('category_key')!r}){signal}"
        )
    return f"{turn.kind.value}{signal}: {turn.reply_text[:80]!r}"


def _record(tally: dict[str, list[int]], scenario: Scenario, ok: bool) -> None:
    """Счёт «провалов из прогонов» по сценарию и языку для итога `--repeat`."""
    counts = tally.setdefault(f"{scenario.kind}/{scenario.language}", [0, 0])
    counts[0] += 0 if ok else 1
    counts[1] += 1


async def _checked_turn(
    provider: LlmProvider, tenant_id: uuid.UUID, scenario: Scenario, consultation: bool
) -> tuple[bool, str]:
    """Один ход сценария с машинной проверкой режима; ошибка API — провал хода."""
    try:
        with tenant_context(tenant_id):
            turn = await orchestrator.handle_message(message=scenario.message, provider=provider)
    except AppError as error:
        # Одна упавшая реплика (отказ модели/ошибка API) не рушит прогон.
        return False, f"ERROR {error.code}: {error.message}"
    if consultation:
        return _check_consultation_turn(scenario, turn)
    return _check_hotel_fact_turn(scenario, turn)


async def _run_group(
    provider: LlmProvider,
    tenant_id: uuid.UUID,
    scenarios: list[Scenario],
    *,
    checked: bool,
    consultation: bool,
    repeat: int,
    tally: dict[str, list[int]],
) -> None:
    """Прогнать группу сценариев `repeat` раз и напечатать исходы.

    `checked=False` — сценарии без машинной проверки в режиме `true` (`request`,
    `high_stakes`, `escalation`): исход печатается для ручной оценки и в итог
    не идёт. В консультациях у тех же сценариев проверка есть.
    """
    for scenario in scenarios:
        for attempt in range(1, repeat + 1):
            tag = f"[{scenario.language}/{scenario.kind}{f' #{attempt}' if repeat > 1 else ''}]"
            if checked:
                ok, got = await _checked_turn(provider, tenant_id, scenario, consultation)
                _record(tally, scenario, ok)
                tag = f"{'OK ' if ok else '!! '}{tag}"
            else:
                try:
                    with tenant_context(tenant_id):
                        turn = await orchestrator.handle_message(
                            message=scenario.message, provider=provider
                        )
                    got = _summarize(turn)
                except AppError as error:
                    got = f"ERROR {error.code}: {error.message}"
            print(
                f"{tag} want: {_expectation(scenario, consultation)}\n"
                f"    msg: {scenario.message}\n"
                f"    got: {got}"
            )


async def _assert_requests(provider: LlmProvider, tenant_id: uuid.UUID) -> list[str]:
    """Сквозной ассерт создания заявки (#71) по всем request-сценариям.

    Проходит весь путь до строки в БД и печатает исход всегда; возвращает
    провалы — жёстко валит прогон по ним только активная модель рантайма.
    """
    failures: list[str] = []
    for scenario in SCENARIOS:
        if scenario.kind != "request":
            continue
        try:
            created, detail = await _assert_request_created(provider, tenant_id, scenario)
        except AppError as error:
            created, detail = False, f"ERROR {error.code}: {error.message}"
        print(f"  {'OK ' if created else '!! '}[{scenario.language}/request] {detail}")
        if not created:
            failures.append(f"[{scenario.language}]: {detail}")
    return failures


async def run(models: list[str], repeat: int) -> None:
    settings = get_settings()
    if not settings.anthropic_api_key:
        print("ANTHROPIC_API_KEY не задан — bake-off требует реального ключа (.env).")
        return

    await _ensure_eval_tenant()
    tenant_id = await _eval_tenant_id()
    consultation = not settings.enable_service_requests

    print("Bake-off: Haiku 4.5 vs Sonnet 5 (§7.7, ADR-010).")
    print("Цена: Haiku $1/$5, Sonnet $3/$15 за Mtok. Оценка исходов — ручная/LLM-judge.")
    mode = "только консультации" if consultation else "заявки включены"
    print(
        f"Активная модель рантайма (LLM_MODEL): {settings.llm_model}; "
        f"ENABLE_SERVICE_REQUESTS={settings.enable_service_requests} ({mode}); "
        f"повторов: {repeat}\n"
    )

    # Регрессия #71 ловится ассертом только на активной модели рантайма: именно
    # её промпт+модель должны надёжно создавать заявку. Остальные кандидаты —
    # информационное сравнение (print), без жёсткого гейта.
    request_failures: list[str] = []
    for model in models:
        provider = build_anthropic_provider(model)
        tally: dict[str, list[int]] = {}
        print(f"\n===== {model} =====")
        await _run_group(
            provider,
            tenant_id,
            SCENARIOS,
            checked=consultation,
            consultation=consultation,
            repeat=repeat,
            tally=tally,
        )
        # Знания об отеле (spec 0036, issue #333/#334): восемь сценариев × ru/kk/en.
        print(f"  --- справочник отеля (spec 0036), {model} ---")
        await _run_group(
            provider,
            tenant_id,
            HOTEL_FACT_SCENARIOS,
            checked=True,
            consultation=consultation,
            repeat=repeat,
            tally=tally,
        )
        if consultation:
            print(
                f"  --- сквозной ассерт заявки НЕ гоняется, {model}: режим консультаций, "
                "заявок нет по построению; его стерегут прогон с "
                "ENABLE_SERVICE_REQUESTS=true и job smoke в CI ---"
            )
        else:
            print(f"  --- сквозной ассерт заявки (два хода → строка в БД), {model} ---")
            failures = await _assert_requests(provider, tenant_id)
            if model == settings.llm_model:
                request_failures += [f"{model} {failure}" for failure in failures]
        print(f"  ==== итог {model} ({mode}): провалов из прогонов ====")
        for key, (failed, total) in tally.items():
            print(f"    {key:30s} {failed}/{total}")

    if request_failures:
        print("\nПРОВАЛ сквозного ассерта заявки на активной модели (баг #71):")
        for failure in request_failures:
            print(f"  - {failure}")
        raise AssertionError(
            f"{len(request_failures)} request-сценарий(ев) активной модели "
            f"{settings.llm_model} не создали заявку — гейт P-9 не сработал (#71)"
        )
    if not consultation:
        print("\nСквозной ассерт заявки на активной модели пройден: все языки создали заявку.")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bake-off моделей гостевого диалога (§7.7).")
    parser.add_argument(
        "--model",
        action="append",
        choices=CANDIDATE_MODELS,
        help="модель-кандидат (флаг повторяемый); по умолчанию — все",
    )
    parser.add_argument("--repeat", type=int, default=1, help="повторов каждого сценария")
    return parser.parse_args(argv)


if __name__ == "__main__":
    arguments = _parse_args(sys.argv[1:])
    try:
        asyncio.run(run(arguments.model or CANDIDATE_MODELS, max(arguments.repeat, 1)))
    except AssertionError as error:
        print(f"\nBAKE-OFF FAILED: {error}")
        sys.exit(1)
