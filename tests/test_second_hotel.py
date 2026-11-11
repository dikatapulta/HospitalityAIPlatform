"""Второй субъект: в базе всегда есть «чужой» отель (#1052, проба мутаций аудита 11.11).

Правило с ключом «на отель» тест одного отеля не проверяет: убери из запроса
фильтр по `tenant_id` — и выборка по-прежнему находит единственный отель базы.
Проба сняла такой фильтр в `mutate_tenant_config` и `staffed_service_keys`, и
весь набор прошёл зелёным. Здесь у каждого теста первым создан соседний отель —
с конфигом и активным сотрудником, — и только потом свой: запрос без фильтра
находит соседа раньше своего и краснеет.

Фикстура `foreign_hotel` — для любого теста правила «на отель»: её берут первой
в списке аргументов, до фикстуры своего отеля.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from hospitality.platform.config import (
    TENANT_CONFIG_SCHEMA_VERSION,
    TenantConfig,
    load_tenant_config,
    mutate_tenant_config,
    store_tenant_config,
)
from hospitality.platform.models import StaffRole, Tenant
from hospitality.platform.staff_auth import (
    ERR_AUTH_FORBIDDEN,
    load_staff_context,
    login,
    require_role,
    resolve_staff_session,
)
from hospitality.platform.staff_team import active_service_member_ids, staffed_service_keys
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context
from tests.test_staff_auth import (
    PASSWORD,
    QUEUE_ROLES,
    _staff_request,
    _unique_ip,
    create_staff_user,
    unique_login,
)

FOREIGN_SERVICE = "laundry"
OWN_SERVICE = "housekeeping"


@dataclass(frozen=True)
class Hotel:
    tenant_id: uuid.UUID
    slug: str
    staff_id: uuid.UUID
    staff_login: str


def _config(fact_topics: list[str]) -> TenantConfig:
    data: dict[str, Any] = {
        "schema_version": TENANT_CONFIG_SCHEMA_VERSION,
        "profile": {"city": "Almaty", "country_code": "KZ"},
        "timezone": "Asia/Almaty",
        "default_language": "ru",
        "hotel_facts": [
            {"topic": topic, "answer": "ответ", "valid_until": None} for topic in fact_topics
        ],
    }
    return TenantConfig.model_validate(data)


async def _hotel(slug: str, *, facts: list[str], role: StaffRole, service: str) -> Hotel:
    async with platform_session_scope() as session:
        tenant = Tenant(slug=slug, name=slug)
        session.add(tenant)
        await session.flush()
        await store_tenant_config(session, tenant.id, _config(facts))
        tenant_id = tenant.id
    staff_login = unique_login()
    staff_id = await create_staff_user(
        staff_login, tenant_id=tenant_id, role=role, service_keys=(service,)
    )
    return Hotel(tenant_id=tenant_id, slug=slug, staff_id=staff_id, staff_login=staff_login)


@pytest.fixture
async def foreign_hotel(canonical_database: None) -> Hotel:
    """Соседний отель, созданный ПЕРВЫМ: конфиг с фактом и активный менеджер службы
    `FOREIGN_SERVICE` — то, что найдёт запрос, потерявший фильтр по отелю."""
    return await _hotel(
        "hotel-foreign", facts=["Завтрак у соседей"], role=StaffRole.MANAGER, service=FOREIGN_SERVICE
    )


@pytest.fixture
async def own_hotel(foreign_hotel: Hotel) -> Hotel:
    """Свой отель — после соседа: без фактов, сотрудник одной службы `OWN_SERVICE`."""
    return await _hotel("hotel-own", facts=[], role=StaffRole.STAFF, service=OWN_SERVICE)


def _appending_fact(topic: str) -> Callable[[TenantConfig], TenantConfig]:
    def mutate(config: TenantConfig) -> TenantConfig:
        return _config([*(fact.topic for fact in config.hotel_facts), topic])

    return mutate


async def _fact_topics(tenant_id: uuid.UUID) -> list[str]:
    async with platform_session_scope() as session:
        config = await load_tenant_config(session, tenant_id)
    return [fact.topic for fact in config.hotel_facts]


async def test_mutate_tenant_config_edits_only_its_own_hotel(
    foreign_hotel: Hotel, own_hotel: Hotel
) -> None:
    """Правка справочника своего отеля не трогает соседний и берёт свой конфиг, а не
    первый в таблице (`mutate_tenant_config`, `Tenant.id == tenant_id`)."""
    async with platform_session_scope() as session:
        updated = await mutate_tenant_config(session, own_hotel.tenant_id, _appending_fact("Wi-Fi"))

    assert [fact.topic for fact in updated.hotel_facts] == ["Wi-Fi"]
    assert await _fact_topics(own_hotel.tenant_id) == ["Wi-Fi"]
    assert await _fact_topics(foreign_hotel.tenant_id) == ["Завтрак у соседей"]


async def test_staffed_services_and_members_are_counted_per_hotel(
    foreign_hotel: Hotel, own_hotel: Hotel
) -> None:
    """Служба соседа со своим сотрудником не делает «укомплектованной» ту же службу
    здесь: иначе лестница эскалации ждала бы сотрудника, которого в отеле нет
    (spec 0045 §4), а раздача этажей ушла бы чужому (spec 0053 §4)."""
    assert await staffed_service_keys(own_hotel.tenant_id) == {OWN_SERVICE}
    assert await staffed_service_keys(foreign_hotel.tenant_id) == {FOREIGN_SERVICE}
    assert await active_service_member_ids(own_hotel.tenant_id, FOREIGN_SERVICE) == frozenset()
    assert await active_service_member_ids(own_hotel.tenant_id, OWN_SERVICE) == {
        own_hotel.staff_id
    }


async def test_staff_does_not_enter_a_hotel_that_has_its_own_active_staff(
    foreign_hotel: Hotel, own_hotel: Hotel
) -> None:
    """Сотрудник своего отеля с живой сессией идёт по адресу соседнего — 403, хотя
    у соседа есть активный менеджер: членство ищется по паре «человек + отель».
    Контекст тенанта — соседский, как его поставил бы резолвер по slug из адреса:
    без него сработала бы сверка контекста, и тест не видел бы поиска членства."""
    grant = await login(
        own_hotel.tenant_id, own_hotel.staff_login, PASSWORD, client_ip=_unique_ip()
    )
    active = await resolve_staff_session(grant.session_token)
    assert active is not None

    assert await load_staff_context(active, foreign_hotel.slug) is None
    with tenant_context(foreign_hotel.tenant_id), pytest.raises(AppError) as error:
        await require_role(*QUEUE_ROLES)(_staff_request(grant.session_token, foreign_hotel.slug))
    assert error.value.code == ERR_AUTH_FORBIDDEN

    own = await load_staff_context(active, own_hotel.slug)
    assert own is not None
    assert (own.tenant_id, own.user_id) == (own_hotel.tenant_id, own_hotel.staff_id)
