"""Фикстуры тестов канала Telegram (Task 0016).

Инфраструктурные фикстуры (временная БД с миграциями, гигиена контекста)
реимпортируются из `tests/conftest.py` — канонический приём (как в gateway,
requests и ai). `demo_tenant` даёт тенанта со slug `demo-hotel` — под него
маппится чат по умолчанию (`TELEGRAM_TENANT_SLUG`); `two_tenants` — для теста
изоляции на таблицах conversations/messages.

F811 отключён на файл: фикстура-параметр обязана называться как реимпортированная
фикстура — так pytest связывает их.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from typing import Any

import pytest

from hospitality.channels.common.consent import CONSENT_VERSION
from hospitality.channels.common.store import ensure_conversation, record_consent
from hospitality.channels.telegram.normalize import CHANNEL
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.platform.models import Tenant
from hospitality.shared.db import platform_session_scope
from hospitality.shared.tenancy import tenant_context
from tests.conftest import (  # noqa: F401  (реимпорт общих фикстур для pytest)
    _clean_log_context,
    _isolated_event_subscribers,
    canonical_database,
    migrated_database_name,
)

# Совпадает с дефолтом Settings.telegram_tenant_slug — маппинг чата Phase 0.
DEMO_TENANT_SLUG = "demo-hotel"


class RecordingSender:
    """Фейк-отправитель (порт TelegramSender): копит отправленное и клавиатуры.

    Общий для тестов уведомлений (Task 0017) и напоминаний (spec 0028) — оба
    проверяют, ЧТО ушло в staff-чат, не трогая Bot API.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.markups: list[dict[str, Any] | None] = []
        self.keyboard_edits: list[tuple[str, str, dict[str, Any] | None]] = []

    async def send_message(
        self, chat_id: str, text: str, *, reply_markup: dict[str, Any] | None = None
    ) -> str | None:
        self.sent.append((chat_id, text))
        self.markups.append(reply_markup)
        return "m" + str(len(self.sent))

    async def answer_callback_query(self, callback_id: str, text: str) -> None:
        return None

    async def edit_message_reply_markup(
        self, chat_id: str, message_id: str, reply_markup: dict[str, Any] | None
    ) -> None:
        self.keyboard_edits.append((chat_id, message_id, reply_markup))


@pytest.fixture
async def demo_tenant(canonical_database: None) -> uuid.UUID:
    """Тенант канала (slug `demo-hotel`) — на него маппится входящий чат."""
    async with platform_session_scope() as session:
        tenant = Tenant(slug=DEMO_TENANT_SLUG, name="Demo Hotel")
        session.add(tenant)
        await session.flush()
        return tenant.id


async def set_staff_routing(tenant_id: uuid.UUID, mapping: dict[str, str]) -> None:
    """Записать тенанту конфиг с маршрутизацией уведомлений по службам (spec 0026).

    Фикстура `demo_tenant` создаёт тенанта БЕЗ конфига (это отдельный сценарий —
    деградация к дефолтному чату), поэтому тесты маршрутизации дозаполняют его
    каноническим путём записи (`store_tenant_config`).
    """
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                staff_chats_by_category=mapping,
            ),
        )


async def grant_consent(tenant_id: uuid.UUID, chat_id: int | str) -> uuid.UUID:
    """Диалог гостя с уже данным согласием (spec 0029): вернуть его id.

    Consent-gate — отдельный сценарий (`test_consent.py`); остальные тесты
    канала проверяют путь ПОСЛЕ гейта, поэтому проходят его заранее, а не
    прокликивают кнопку в каждом. Пишется каноническим путём (`record_consent`),
    а не INSERT'ом мимо него.
    """
    with tenant_context(tenant_id):
        conversation_id = await ensure_conversation(CHANNEL, str(chat_id))
        await record_consent(conversation_id, CONSENT_VERSION)
        return conversation_id


@pytest.fixture
async def two_tenants(canonical_database: None) -> tuple[uuid.UUID, uuid.UUID]:
    """Два тенанта в реестре — «Hotel A» и «Hotel B» (канон test_tenant_isolation)."""
    async with platform_session_scope() as session:
        tenant_a = Tenant(slug="hotel-a", name="Hotel A")
        tenant_b = Tenant(slug="hotel-b", name="Hotel B")
        session.add_all([tenant_a, tenant_b])
        await session.flush()
        return (tenant_a.id, tenant_b.id)
