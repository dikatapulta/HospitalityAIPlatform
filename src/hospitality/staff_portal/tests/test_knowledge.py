"""Страница «Справочник отеля» (spec 0036 §7, §8.1, §11 «Кабинет»; issue #335).

Роль, три действия (добавить, изменить с переименованием, удалить), десять
отказов §8.1 — каждый `ERR-PLATFORM-009` со своей надписью, а не 422 и не 500,
тема с пробелами и кириллицей правится и удаляется (тема едет телом, не путём),
счётчик бюджета совпадает с суммой. Сами пределы схемы и гонка двух менеджеров
проверены у владельца — `tests/test_tenant_config.py`.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import httpx
import pytest
from httpx import AsyncClient

from hospitality.platform.config import HotelFact, load_tenant_config, mutate_tenant_config
from hospitality.platform.models import StaffRole
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.humanize import month_day_ru
from hospitality.staff_portal.tests.conftest import (
    HOTEL_SLUG,
    PortalHotel,
    store_hotel_config,
    submit_login,
)
from tests.test_staff_auth import create_staff_user, unique_login

SAME_ORIGIN = {"origin": "https://test"}
PAGE = f"/staff/{HOTEL_SLUG}/knowledge"
FACTS = f"/staff/{HOTEL_SLUG}/api/knowledge/facts"
REJECTED = "ERR-PLATFORM-009"


async def _post(client: AsyncClient, path: str, payload: dict[str, object]) -> httpx.Response:
    """POST по CSRF-контракту JSON-действий: JSON-тип + same-origin Origin."""
    return await client.post(path, json=payload, headers=SAME_ORIGIN)


async def _set_facts(tenant_id: uuid.UUID, facts: list[HotelFact]) -> None:
    async with platform_session_scope() as session:
        await mutate_tenant_config(
            session, tenant_id, lambda config: config.model_copy(update={"hotel_facts": facts})
        )


async def _facts(tenant_id: uuid.UUID) -> list[HotelFact]:
    async with platform_session_scope() as session:
        return list((await load_tenant_config(session, tenant_id)).hotel_facts)


@pytest.fixture
async def manager(client: AsyncClient, portal_hotel: PortalHotel) -> PortalHotel:
    """Менеджер вошёл, у отеля есть конфиг со справочником «Завтрак»."""
    await store_hotel_config(portal_hotel.tenant_id)
    await _set_facts(
        portal_hotel.tenant_id,
        [HotelFact(topic="Завтрак", answer="С 07:00 до 10:30 на 2 этаже")],
    )
    await submit_login(client, portal_hotel.login)
    return portal_hotel


async def test_page_and_actions_require_manager(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """docs/RBAC.md: справочник — только менеджеру; страница и действия — одной
    ролью (README: страница без действия или наоборот — дыра)."""
    await store_hotel_config(portal_hotel.tenant_id)
    staff_login = unique_login()
    await create_staff_user(
        staff_login, tenant_id=portal_hotel.tenant_id, role=StaffRole.RECEPTIONIST
    )
    await submit_login(client, staff_login)

    page = await client.get(PAGE)
    added = await _post(client, FACTS, {"topic": "Wi-Fi", "answer": "Сеть Hotel"})

    assert page.status_code == 403
    assert "Нет доступа" in page.text
    assert added.status_code == 403
    assert added.json()["error"]["code"] == "ERR-AUTH-003"
    assert await _facts(portal_hotel.tenant_id) == []


async def test_actions_require_json_same_origin(client: AsyncClient, manager: PortalHotel) -> None:
    response = await client.post(FACTS, json={"topic": "Wi-Fi", "answer": "Сеть Hotel"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ERR-AUTH-009"


async def test_page_shows_texts_counter_and_labels(
    client: AsyncClient, manager: PortalHotel
) -> None:
    """Надписи §8.1, пометки временного и просроченного, счётчик = сумма знаков."""
    later = utc_now().date() + timedelta(days=30)
    facts = [
        HotelFact(topic="Завтрак", answer="С 07:00 до 10:30 на 2 этаже"),
        HotelFact(topic="Бассейн", answer="Закрыт на ремонт", valid_until=later),
        HotelFact(topic="Лифт", answer="Работает один", valid_until=date(2026, 1, 5)),
    ]
    await _set_facts(manager.tenant_id, facts)

    response = await client.get(PAGE)

    assert response.status_code == 200
    body = response.text
    used = sum(len(fact.topic) + len(fact.answer) for fact in facts)
    assert f"Занято {used} из 3 000 знаков" in body
    assert "3 из 30 фактов" in body
    assert "Справочник читает бот в каждом ответе гостю" in body
    assert f"до {month_day_ru(later)}" in body
    assert "срок истёк — гости этого не видят" in body
    for label in ("Тема", "Ответ гостю", "Действует до", "Добавить", "Изменить", "Удалить"):
        assert label in body
    # Порядок хранения: как расставил отель, а не по алфавиту.
    assert body.index("Завтрак") < body.index("Бассейн") < body.index("Лифт")


async def test_empty_reference_says_so(client: AsyncClient, manager: PortalHotel) -> None:
    await _set_facts(manager.tenant_id, [])
    response = await client.get(PAGE)
    assert "Справочник пуст." in response.text
    assert "Занято 0 из 3 000 знаков · 0 из 30 фактов" in response.text


async def test_add_update_with_rename_and_delete(client: AsyncClient, manager: PortalHotel) -> None:
    """Тема с пробелами и кириллицей правится и удаляется; правка оставляет факт
    на его месте, новый — в конце (порядок расставляет менеджер)."""
    later = utc_now().date() + timedelta(days=10)
    added = await _post(
        client, FACTS, {"topic": "  Спа и сауна ", "answer": " С 10 до 22 ", "valid_until": None}
    )
    assert added.status_code == 200
    assert (await _post(client, FACTS, {"topic": "Wi-Fi", "answer": "Сеть Hotel"})).is_success

    renamed = await _post(
        client,
        f"{FACTS}/update",
        {
            "topic": "спа и сауна",
            "new_topic": "Спа",
            "answer": "С 10:00 до 22:00, 3 этаж",
            "valid_until": later.isoformat(),
        },
    )
    assert renamed.status_code == 200
    assert [(f.topic, f.answer, f.valid_until) for f in await _facts(manager.tenant_id)] == [
        ("Завтрак", "С 07:00 до 10:30 на 2 этаже", None),
        ("Спа", "С 10:00 до 22:00, 3 этаж", later),
        ("Wi-Fi", "Сеть Hotel", None),
    ]

    deleted = await _post(client, f"{FACTS}/delete", {"topic": "Спа"})
    assert deleted.status_code == 200
    assert [f.topic for f in await _facts(manager.tenant_id)] == ["Завтрак", "Wi-Fi"]


async def test_update_keeps_its_own_topic_with_another_case(
    client: AsyncClient, manager: PortalHotel
) -> None:
    """Правка без переименования — не «тема занята»: занята она самим фактом."""
    response = await _post(
        client, f"{FACTS}/update", {"topic": "Завтрак", "new_topic": "завтрак", "answer": "Нет"}
    )
    assert response.status_code == 200
    assert [f.topic for f in await _facts(manager.tenant_id)] == ["завтрак"]


_LONG_TOPIC = "Т" * 41
_LONG_ANSWER = "о" * 301


@pytest.mark.parametrize(
    ("path", "payload", "message"),
    [
        (
            "",
            {"topic": " ", "answer": "Да"},
            "Напишите тему — одно-два слова: «Завтрак», «Wi-Fi»",
        ),
        (
            "",
            {"topic": "Wi-Fi", "answer": "  "},
            "Напишите ответ гостю — то, что бот скажет на этот вопрос",
        ),
        (
            "",
            {"topic": "Обмен валюты / банкомат", "answer": "В холле"},
            "В теме нельзя использовать «/» — напишите «Обмен валюты и банкомат»",
        ),
        (
            "",
            {"topic": _LONG_TOPIC, "answer": "Да"},
            "Тема длиннее 40 знаков (сейчас 41). Тема — это заголовок, подробности идут в ответ",
        ),
        (
            "",
            {"topic": "Wi-Fi", "answer": _LONG_ANSWER},
            "Ответ длиннее 300 знаков (сейчас 301). Это ответ гостю, а не инструкция службе — "
            "оставьте главное",
        ),
        (
            "",
            {"topic": "Wi-Fi", "answer": "Да", "valid_until": "2025-12-31"},
            "Дата «Действует до» уже прошла — такой факт гости не увидят. Проверьте год",
        ),
        (
            "",
            {"topic": " завтрак ", "answer": "Да"},
            "Такая тема уже есть — откройте её и измените ответ",
        ),
        (
            "/update",
            {"topic": "Ужин", "answer": "Да"},
            "Этот факт уже удалил кто-то из коллег — обновите страницу",
        ),
        (
            "/delete",
            {"topic": "Ужин"},
            "Этот факт уже удалил кто-то из коллег — обновите страницу",
        ),
    ],
)
async def test_each_rejection_has_its_own_message(
    client: AsyncClient,
    manager: PortalHotel,
    path: str,
    payload: dict[str, object],
    message: str,
) -> None:
    """Механизм §7: 409 с надписью §8.1 в `message`, а не 422 и не 500."""
    response = await _post(client, f"{FACTS}{path}", payload)

    assert response.status_code == 409
    error = response.json()["error"]
    assert (error["code"], error["message"]) == (REJECTED, message)
    assert [f.topic for f in await _facts(manager.tenant_id)] == ["Завтрак"]


async def test_rename_onto_another_topic_is_taken(
    client: AsyncClient, manager: PortalHotel
) -> None:
    await _post(client, FACTS, {"topic": "Wi-Fi", "answer": "Сеть Hotel"})
    response = await _post(
        client, f"{FACTS}/update", {"topic": "Wi-Fi", "new_topic": "ЗАВТРАК", "answer": "Да"}
    )
    assert response.status_code == 409
    assert response.json()["error"]["message"] == (
        "Такая тема уже есть — откройте её и измените ответ"
    )


async def test_full_reference_by_count(client: AsyncClient, manager: PortalHotel) -> None:
    await _set_facts(
        manager.tenant_id, [HotelFact(topic=f"Т{index}", answer="да") for index in range(30)]
    )
    response = await _post(client, FACTS, {"topic": "Wi-Fi", "answer": "Сеть Hotel"})
    assert response.status_code == 409
    assert response.json()["error"]["message"] == (
        "Справочник заполнен: 30 фактов — предел. Удалите ненужное"
    )


async def test_full_reference_by_chars_names_the_free_space(
    client: AsyncClient, manager: PortalHotel
) -> None:
    """Число в отказе — само исправление: сколько знаков свободно (§8.1).
    9 × (2 + 300) = 2 718 занято, свободно 282; новый факт — 5 + 290 = 295."""
    await _set_facts(
        manager.tenant_id, [HotelFact(topic=f"Т{index}", answer="о" * 300) for index in range(9)]
    )
    response = await _post(client, FACTS, {"topic": "Новая", "answer": "о" * 290})
    assert response.status_code == 409
    assert response.json()["error"]["message"] == (
        "Не хватает места: свободно 282 знаков из 3 000. Сократите ответ или удалите ненужный факт"
    )


async def test_update_measures_free_space_without_the_edited_fact(
    client: AsyncClient, manager: PortalHotel
) -> None:
    """Правка заменяет факт, а не добавляет второй: его прежние знаки свободны."""
    await _set_facts(
        manager.tenant_id, [HotelFact(topic=f"Т{index}", answer="о" * 298) for index in range(10)]
    )
    response = await _post(client, f"{FACTS}/update", {"topic": "Т0", "answer": "п" * 298})
    assert response.status_code == 200
