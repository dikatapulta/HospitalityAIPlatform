"""Страница «Справочник отеля» (spec 0036 §7, §8.1; issue #335): данные и правки.

Факты живут в `TenantConfig.hotel_facts` (#333); правка — только через
`mutate_tenant_config` (SELECT … FOR UPDATE), поэтому функции правки ниже —
чистые «конфиг → конфиг» и исполняются под блокировкой строки тенанта.

Пределы и запреты §3 проверяются ЗДЕСЬ, до записи, а не схемой тела запроса и
не падением `HotelFact`: каждой причине отказа §8.1 — своя надпись менеджеру,
одним кодом `ERR-PLATFORM-009` (409), надпись — в `message` конверта (§7).
Схема `TenantConfig` остаётся последней линией обороны для путей мимо страницы.

Раздел «Гости спрашивали, а ответа не было» (§7 п. 3, issue #334) — только
чтение: строки `unanswered_questions` пишет канал, читает их владелец таблицы
(`channels/common/unanswered_questions.py`), а окно и предел задаёт страница.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, timedelta
from typing import Any, Final

from hospitality.channels.common.unanswered_questions import list_unanswered_questions
from hospitality.platform.config import (
    MAX_HOTEL_FACT_ANSWER_LENGTH,
    MAX_HOTEL_FACT_TOPIC_LENGTH,
    MAX_HOTEL_FACTS,
    MAX_HOTEL_FACTS_TOTAL_CHARS,
    HotelFact,
    TenantConfig,
    hotel_facts_total_chars,
)
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.db import utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.humanize import month_day_ru
from hospitality.staff_portal import queue

# Код каталога (docs/runbooks/errors.md): правка справочника отклонена. Один код
# на все причины §8.1 — различаются сообщением (прецедент — ERR-AUTH-004).
ERR_HOTEL_FACT_REJECTED: Final = "ERR-PLATFORM-009"

# Раздел «Гости спрашивали, а ответа не было» (§7 п. 3): окно стирает список
# само — ни статуса, ни «скрыть» у строки нет (§6).
UNANSWERED_WINDOW: Final = timedelta(days=7)
UNANSWERED_LIMIT: Final = 20

_NBSP: Final = " "


def _number(value: int) -> str:
    """«1 240», «3 000» — разряды неразрывным пробелом, как пишут по-русски."""
    return f"{value:,}".replace(",", _NBSP)


def _reject(message: str) -> AppError:
    return AppError(code=ERR_HOTEL_FACT_REJECTED, message=message, status_code=409)


def _key(topic: str) -> str:
    """Идентичность факта — тема без учёта регистра и крайних пробелов (§3)."""
    return topic.strip().casefold()


def hotel_today(config: TenantConfig) -> date:
    """Сегодня по поясу отеля — граница «срок истёк» и «дата в прошлом»."""
    return utc_now().astimezone(config.tzinfo).date()


def _checked_fact(topic: str, answer: str, valid_until: date | None, today: date) -> HotelFact:
    """Один факт — проверки §3 в порядке полей формы, надписи §8.1."""
    topic, answer = topic.strip(), answer.strip()
    if not topic:
        raise _reject("Напишите тему — одно-два слова: «Завтрак», «Wi-Fi»")
    if not answer:
        raise _reject("Напишите ответ гостю — то, что бот скажет на этот вопрос")
    if "/" in topic:
        raise _reject("В теме нельзя использовать «/» — напишите «Обмен валюты и банкомат»")
    if len(topic) > MAX_HOTEL_FACT_TOPIC_LENGTH:
        raise _reject(
            f"Тема длиннее {MAX_HOTEL_FACT_TOPIC_LENGTH} знаков (сейчас {len(topic)}). "
            "Тема — это заголовок, подробности идут в ответ"
        )
    if len(answer) > MAX_HOTEL_FACT_ANSWER_LENGTH:
        raise _reject(
            f"Ответ длиннее {MAX_HOTEL_FACT_ANSWER_LENGTH} знаков (сейчас {len(answer)}). "
            "Это ответ гостю, а не инструкция службе — оставьте главное"
        )
    if valid_until is not None and valid_until < today:
        raise _reject("Дата «Действует до» уже прошла — такой факт гости не увидят. Проверьте год")
    return HotelFact(topic=topic, answer=answer, valid_until=valid_until)


def _with_fact(others: list[HotelFact], fact: HotelFact, position: int) -> list[HotelFact]:
    """Справочник с фактом на месте `position` — проверки, которым нужен ВЕСЬ
    справочник: занятая тема и оба переполнения (видны только под блокировкой)."""
    if any(_key(other.topic) == _key(fact.topic) for other in others):
        raise _reject("Такая тема уже есть — откройте её и измените ответ")
    if len(others) + 1 > MAX_HOTEL_FACTS:
        raise _reject(f"Справочник заполнен: {MAX_HOTEL_FACTS} фактов — предел. Удалите ненужное")
    free = max(MAX_HOTEL_FACTS_TOTAL_CHARS - hotel_facts_total_chars(others), 0)
    if hotel_facts_total_chars([fact]) > free:
        raise _reject(
            f"Не хватает места: свободно {_number(free)} знаков из "
            f"{_number(MAX_HOTEL_FACTS_TOTAL_CHARS)}. Сократите ответ или удалите ненужный факт"
        )
    return [*others[:position], fact, *others[position:]]


def _replace_facts(config: TenantConfig, facts: list[HotelFact]) -> TenantConfig:
    """Конфиг с новым справочником — ЧЕРЕЗ схему: `model_copy` валидаторы не
    зовёт, а схема `TenantConfig` — последняя линия обороны (§7)."""
    return TenantConfig.model_validate({**config.model_dump(), "hotel_facts": facts})


def _position(config: TenantConfig, topic: str) -> int:
    """Место факта в справочнике; исчез (удалил коллега) — дружелюбный отказ."""
    for index, fact in enumerate(config.hotel_facts):
        if _key(fact.topic) == _key(topic):
            return index
    raise _reject("Этот факт уже удалил кто-то из коллег — обновите страницу")


def adding(
    topic: str, answer: str, valid_until: date | None
) -> Callable[[TenantConfig], TenantConfig]:
    """Правка «добавить» для `mutate_tenant_config`: новый факт — в конец."""

    def mutate(config: TenantConfig) -> TenantConfig:
        fact = _checked_fact(topic, answer, valid_until, hotel_today(config))
        facts = _with_fact(list(config.hotel_facts), fact, len(config.hotel_facts))
        return _replace_facts(config, facts)

    return mutate


def updating(
    topic: str, new_topic: str | None, answer: str, valid_until: date | None
) -> Callable[[TenantConfig], TenantConfig]:
    """Правка «изменить» (в т.ч. переименовать тему): факт остаётся на своём месте."""

    def mutate(config: TenantConfig) -> TenantConfig:
        position = _position(config, topic)
        fact = _checked_fact(
            topic if new_topic is None else new_topic, answer, valid_until, hotel_today(config)
        )
        others = [f for index, f in enumerate(config.hotel_facts) if index != position]
        return _replace_facts(config, _with_fact(others, fact, position))

    return mutate


def deleting(topic: str) -> Callable[[TenantConfig], TenantConfig]:
    """Правка «удалить»: исчезнувший факт — отказ §8.1, а не 500."""

    def mutate(config: TenantConfig) -> TenantConfig:
        position = _position(config, topic)
        facts = [f for index, f in enumerate(config.hotel_facts) if index != position]
        return _replace_facts(config, facts)

    return mutate


def _fact_view(fact: HotelFact, today: date) -> dict[str, Any]:
    expired = fact.valid_until is not None and fact.valid_until < today
    if fact.valid_until is None:
        label = None
    elif expired:
        label = "срок истёк — гости этого не видят"
    else:
        label = f"до {month_day_ru(fact.valid_until)}"
    return {
        "topic": fact.topic,
        "answer": fact.answer,
        "valid_until": fact.valid_until.isoformat() if fact.valid_until else "",
        "label": label,
        "expired": expired,
    }


async def build_knowledge_context(staff: StaffContext) -> dict[str, Any]:
    """Контекст `knowledge.html`: факты в порядке хранения, счётчик бюджета и
    вопросы гостей без ответа за окно `UNANSWERED_WINDOW`.

    Тенант без конфига (онбординг не завершён) — пустой справочник: страница
    открывается, а правку отклонит `mutate_tenant_config` (ERR-PLATFORM-005).
    """
    config = await queue.tenant_config(staff)
    facts = list(config.hotel_facts) if config is not None else []
    today = hotel_today(config) if config is not None else utc_now().date()
    questions = await list_unanswered_questions(
        created_after=utc_now() - UNANSWERED_WINDOW, limit=UNANSWERED_LIMIT
    )
    tz = config.tzinfo if config is not None else UTC
    return {
        "tenant_name": staff.tenant_name,
        "tenant_slug": staff.tenant_slug,
        "actions_endpoint": f"/staff/{staff.tenant_slug}/api/knowledge/facts",
        "facts": [_fact_view(fact, today) for fact in facts],
        # Счётчик §8.1: делает видимой цену, которую иначе никто не видит.
        "budget": (
            f"Занято {_number(hotel_facts_total_chars(facts))} из "
            f"{_number(MAX_HOTEL_FACTS_TOTAL_CHARS)} знаков · "
            # «2 из 30 фактов», а не «2 фактов из 30» (§8.1): после «из 30»
            # родительный падеж верен при любом числе, склонять не нужно.
            f"{len(facts)} из {MAX_HOTEL_FACTS} фактов"
        ),
        # Дата — по поясу отеля (FOUNDATION §9): вопрос в 01:00 по Алматы
        # иначе встал бы вчерашним числом.
        "unanswered": [
            {
                "question": item.question,
                "day": month_day_ru(item.created_at.astimezone(tz).date()),
            }
            for item in questions
        ],
    }
