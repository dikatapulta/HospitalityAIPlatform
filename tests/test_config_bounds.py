"""Границы числовых полей `Settings` (issue #283, приём #273).

Каждое числовое поле размечено ровно одним классом ниже. Новое поле без
класса роняет `test_every_numeric_field_is_classified`: решить, ломает ли
его ноль рантайм, обязан тот, кто поле заводит, — иначе граница ставится
по инерции или забывается, а 0 в `.env` вскрывается у гостя, а не на старте.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hospitality.shared.config import Settings

# Ноль — отказ рантайма (холостой цикл, деление на ноль, мгновенный срок,
# стёртая переписка гостей): старт обязан падать.
ZERO_REJECTED = frozenset(
    {
        "postgres_port",
        "redis_port",
        "app_port",
        "clamav_port",
        "worker_poll_interval_seconds",
        "worker_batch_size",
        "worker_max_delivery_attempts",
        "worker_retry_backoff_base_seconds",
        "worker_retry_backoff_max_seconds",
        "worker_delivery_lease_seconds",
        "outbox_retention_days",
        "messages_retention_days",
        "worker_staff_push_interval_seconds",
        "llm_timeout_seconds",
        "llm_max_attempts",
        "guest_chat_rate_limit_window_seconds",
        "staff_login_rate_limit_window_seconds",
        "guest_code_verify_rate_limit_window_seconds",
        "guest_bind_link_issue_rate_limit_window_seconds",
        "guest_bind_link_consume_rate_limit_window_seconds",
        "staff_invite_issue_rate_limit_window_seconds",
        "staff_session_idle_ttl_days",
        "staff_session_absolute_ttl_days",
        "staff_session_short_idle_minutes",
        "staff_invite_ttl_hours",
        "staff_superadmin_session_ttl_minutes",
        "staff_password_reset_ttl_hours",
        "alert_poll_interval_seconds",
        "alert_ready_failure_threshold",
        "alert_error_spike_threshold",
        "alert_worker_heartbeat_max_age_seconds",
        "alert_outbox_depth_threshold",
        "alert_backup_max_age_hours",
        "alert_disk_min_free_ratio",
        "alert_guest_text_retention_grace_hours",
    }
)

# Ноль законен — шаг цикла каждым кругом, стоп-кран бюджета, повтор без паузы,
# выключенная линия алерта; отрицательное смысла не имеет и падает.
ZERO_ALLOWED = frozenset(
    {
        "worker_cleanup_interval_seconds",
        "worker_dead_letter_alert_interval_seconds",
        "worker_heartbeat_interval_seconds",
        "worker_retention_interval_seconds",
        "worker_reminder_interval_seconds",
        "worker_daily_summary_interval_seconds",
        "worker_stay_auto_checkout_interval_seconds",
        "worker_pms_sync_interval_seconds",
        "llm_retry_backoff_base_seconds",
        "llm_tenant_daily_budget_usd",
        "llm_mentor_daily_budget_usd",
        "llm_documents_daily_budget_usd",
        "llm_showroom_daily_budget_usd",
        "llm_ideas_daily_budget_usd",
        "alert_cooldown_seconds",
        "alert_login_rate_limited_threshold",
        "alert_llm_failure_streak_threshold",
        "alert_guest_door_threshold",
    }
)

# ≤0 — задокументированный выключатель (страховочный люк лимитов): границы нет.
UNBOUNDED = frozenset(
    {
        "guest_chat_rate_limit_messages",
        "guest_chat_rate_limit_messages_per_day",
        "guest_chat_daily_budget_share",
        "staff_login_rate_limit_attempts",
        "staff_login_ip_rate_limit_attempts",
        "staff_login_ip_flood_rate_limit_attempts",
        "guest_code_verify_rate_limit_attempts",
        "guest_code_verify_ip_rate_limit_attempts",
        "guest_bind_link_issue_rate_limit_attempts",
        "guest_bind_link_consume_rate_limit_attempts",
        "staff_invite_issue_rate_limit_attempts",
        "alert_llm_budget_ratio",
    }
)


def test_every_numeric_field_is_classified() -> None:
    numeric = {
        name for name, info in Settings.model_fields.items() if info.annotation in (int, float)
    }

    assert not ZERO_REJECTED & ZERO_ALLOWED
    assert not (ZERO_REJECTED | ZERO_ALLOWED) & UNBOUNDED
    assert numeric == ZERO_REJECTED | ZERO_ALLOWED | UNBOUNDED, (
        "числовое поле Settings без класса границы (или класс без поля) — "
        "впиши его в один из классов этого файла (#283)"
    )


@pytest.mark.parametrize("field", sorted(ZERO_REJECTED))
def test_zero_fails_on_start(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    monkeypatch.setenv(field.upper(), "0")

    with pytest.raises(ValidationError) as exc_info:
        Settings()

    assert field in str(exc_info.value)


@pytest.mark.parametrize("field", sorted(ZERO_ALLOWED))
def test_zero_starts_and_negative_fails(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    monkeypatch.setenv(field.upper(), "0")
    assert getattr(Settings(), field) == 0

    monkeypatch.setenv(field.upper(), "-1")
    with pytest.raises(ValidationError) as exc_info:
        Settings()

    assert field in str(exc_info.value)


@pytest.mark.parametrize("field", sorted(UNBOUNDED))
def test_switch_accepts_negative(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    monkeypatch.setenv(field.upper(), "-1")

    assert getattr(Settings(), field) == -1


@pytest.mark.parametrize("field", ["postgres_port", "redis_port", "app_port", "clamav_port"])
def test_port_above_range_fails_on_start(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    monkeypatch.setenv(field.upper(), "65536")

    with pytest.raises(ValidationError):
        Settings()
