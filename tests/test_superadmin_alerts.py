"""Уведомления о суперадмине (spec 0044 §6): текст без ПД, доставка, без бота."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from hospitality.platform.events import SuperadminActed, SuperadminActionKind
from hospitality.platform.models import StaffRole, Tenant
from hospitality.platform.superadmin_alerts import (
    build_superadmin_alert_handler,
    format_superadmin_alert,
    runbook_url,
)
from hospitality.shared.db import platform_session_scope
from hospitality.shared.tenancy import tenant_context

ALMATY = ZoneInfo("Asia/Almaty")
MOMENT = datetime(2026, 10, 24, 9, 5, tzinfo=UTC)  # 14:05 в Алматы


def _event(kind: SuperadminActionKind, role: StaffRole | None = None) -> SuperadminActed:
    return SuperadminActed(
        kind=kind,
        occurred_at=MOMENT,
        actor_user_id=uuid.uuid4(),
        target_user_id=uuid.uuid4() if role else None,
        target_role=role,
    )


def _text(event: SuperadminActed) -> str:
    return format_superadmin_alert(
        event, hotel="Казахстан", zone=ALMATY, environment="staging", runbook="RB"
    )


def test_login_alert_names_hotel_and_local_time() -> None:
    assert _text(_event(SuperadminActionKind.LOGIN)) == (
        "🔐 [staging] Вход суперадмина — «Казахстан», 24.10 14:05 по времени отеля.\nНе вы? RB"
    )


def test_bad_code_alert_says_password_is_known() -> None:
    text = _text(_event(SuperadminActionKind.LOGIN_BAD_CODE))
    assert text.startswith("⚠️ [staging] Суперадмин: пароль верный, код из приложения — нет")
    assert "Если это не вы, пароль знает кто-то ещё: RB" in text


def test_action_alerts_carry_role_not_identity() -> None:
    reset = _event(SuperadminActionKind.PASSWORD_RESET_ISSUED, StaffRole.RECEPTIONIST)
    erase = _event(SuperadminActionKind.MEMBER_ERASED, StaffRole.STAFF)
    assert "сотруднику с ролью «Ресепшен»" in _text(reset)
    assert "стёр учётку с ролью «Сотрудник»" in _text(erase)
    # Ни логинов, ни имён, ни id в тексте нет — только роль (ПД не уходят в Telegram).
    for event in (reset, erase):
        assert str(event.target_user_id) not in _text(event)
        assert str(event.actor_user_id) not in _text(event)


@pytest.mark.parametrize(
    ("kind", "phrase"),
    [
        (SuperadminActionKind.CREATED_ON_SERVER, "На сервере заведён суперадмин"),
        (SuperadminActionKind.FACTOR_RESET_ON_SERVER, "заменён код из приложения"),
        (SuperadminActionKind.PASSWORD_SET_ON_SERVER, "сменён пароль суперадмина"),
    ],
)
def test_server_command_alerts(kind: SuperadminActionKind, phrase: str) -> None:
    assert phrase in _text(_event(kind))


def test_runbook_link_sits_next_to_alerts_runbook() -> None:
    assert runbook_url().endswith("/docs/runbooks/superadmin.md#если-вход-или-действие-не-ваши")


async def test_handler_sends_text_with_tenant_name(canonical_database: None) -> None:
    async with platform_session_scope() as session:
        tenant = Tenant(slug="hotel-alert", name="Отель Алерт")
        session.add(tenant)
        await session.flush()
        tenant_id = tenant.id
    sent: list[str] = []

    async def sender(text: str) -> None:
        sent.append(text)

    with tenant_context(tenant_id):
        await build_superadmin_alert_handler(sender)(_event(SuperadminActionKind.LOGIN))

    assert len(sent) == 1
    # Конфига у тенанта нет — время честно в UTC.
    assert "«Отель Алерт», 24.10 09:05 по времени отеля" in sent[0]


async def test_handler_without_alert_bot_only_warns() -> None:
    await build_superadmin_alert_handler(None)(_event(SuperadminActionKind.LOGIN))
