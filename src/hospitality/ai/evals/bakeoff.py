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
"""

from __future__ import annotations

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
    # факта, обязанное дойти до ГОСТЯ, и корни тем, обязанные дойти до
    # МЕНЕДЖЕРА строкой справочника (сравнение без учёта регистра).
    fact_value: str | None = None
    question_topics: tuple[str, ...] = ()


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
# hotel_fact_unknown — ждём честное «нет в справке» + сигнал, БЕЗ обещания
# сотрудника (§6: обещать некому, а после #101 это сделало бы эскалацией каждый
# неизвестный факт);
# hotel_fact_unknown_mixed — ждём и сигнал, и заявку: реплика сигнала первым
# абзацем, confirmation_question остаётся ЧИСТЫМ вопросом-подтверждением;
# hotel_fact_partial — вопрос покрыт + вопрос не покрыт, действия нет: значение
# факта обязано дойти до гостя (гость видит только `reply_to_guest`, и без
# правила описаний модель клала туда лишь «про утюг не знаю» — 17 из 20);
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
    ),
    Scenario(
        "kk",
        "hotel_fact_mixed",
        "Таңғы ас нешеде және 305-ке сүлгі әкеліңізші",
        "tool + ответ про завтрак В НАЧАЛЕ confirmation_question (kk!)",
    ),
    Scenario(
        "en",
        "hotel_fact_mixed",
        "What time is breakfast, and please bring towels to 305",
        "tool + ответ про завтрак В НАЧАЛЕ confirmation_question",
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
    ),
    Scenario(
        "kk",
        "hotel_fact_partial",
        "Wi-Fi құпия сөзі қандай және бөлмеде үтік бар ма?",
        "пароль ДОШЁЛ + про утюг нет в справке + сигнал (kk!)",
        fact_value="welcome2026",
    ),
    Scenario(
        "en",
        "hotel_fact_partial",
        "What's the Wi-Fi password, and is there an iron in the room?",
        "пароль ДОШЁЛ + про утюг нет в справке + сигнал",
        fact_value="welcome2026",
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
    ответу — автоматически это не проверить. Зато проверяется машинно то, чего
    не проверит и юнит-тест (он подставляет аргументы модели сам): на смешанном
    ходу ответ обязан лежать В НАЧАЛЕ `confirmation_question` инструмента, а не
    в свободном тексте — свободный текст гейт P-9 гостю не показывает вовсе,
    и без правила промпта v5 ответ про завтрак пропал бы молча (§6).
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

    if scenario.kind != "hotel_fact_mixed":
        # Простой вопрос: инструмента быть не должно, ответ — обычной репликой.
        if turn.pending_action is not None:
            return False, f"вместо ответа вызван инструмент {turn.pending_action.tool_name}"
        return _check_language(scenario, turn.reply_text)
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
    вопросы без факта дошли до МЕНЕДЖЕРА строкой справочника.
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

        # Знания об отеле (spec 0036, issue #333/#334): восемь сценариев × ru/kk/en.
        print(f"  --- справочник отеля (spec 0036), {model} ---")
        for scenario in HOTEL_FACT_SCENARIOS:
            try:
                with tenant_context(tenant_id):
                    turn = await orchestrator.handle_message(
                        message=scenario.message, provider=provider
                    )
                ok, detail = _check_hotel_fact_turn(scenario, turn)
            except AppError as error:
                ok, detail = False, f"ERROR {error.code}: {error.message}"
            print(
                f"  {'OK ' if ok else '!! '}[{scenario.language}/{scenario.kind}] "
                f"want: {scenario.expectation}\n"
                f"      msg: {scenario.message}\n"
                f"      got: {detail}"
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
