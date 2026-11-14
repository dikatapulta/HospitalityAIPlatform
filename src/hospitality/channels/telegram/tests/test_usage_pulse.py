"""«Пульс пользы» — строки копии основателю и сигнал «тихий день» (spec 0035 §8.1–8.2).

Числа считают владельцы (их тесты — у владельцев: `test_staff_logins.py`, `guests/tests/
test_logins.py`, `mentor/tests/test_activity.py`); здесь — сборка и условия. Данные —
каноническими путями, в настоящей базе; «есть ли назначенный экзамен» подменяется: путь
назначения требует загруженного кодекса, а сам ответ проверен у владельца.

Часы прогона подменяются ВНУТРИ модуля, от НАСТОЯЩЕГО дня отеля (канон
`test_daily_summary.py`): данные пишутся по реальным часам, и окно обязано их видеть.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from hospitality.channels.common.reply_language import ReplyLanguageCount, check_reply_language
from hospitality.channels.telegram import daily_summary, usage_pulse
from hospitality.channels.telegram.daily_summary import send_daily_summaries
from hospitality.channels.telegram.tests.conftest import RecordingSender, bind_chat
from hospitality.channels.telegram.usage_pulse import (
    UsagePulse,
    pulse_lines,
    send_quiet_day_alerts,
)
from hospitality.modules.mentor import api as mentor_api
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.platform.models import StaffRole, StaffSession, TenantMembership, User
from hospitality.platform.staff_logins import StaffLogins
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.tenancy import current_tenant_id, tenant_context

ALERT_CHAT = "-100999"
ZONE = ZoneInfo("Asia/Almaty")


class RecordingAlerts:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, text: str) -> None:
        self.sent.append(text)


async def _configure(
    tenant_id: uuid.UUID, *, mentor_enabled: bool = True, hotel_chat: str | None = None
) -> None:
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                mentor_enabled=mentor_enabled,
                daily_summary_chat_id=hotel_chat,
            ),
        )


async def _staff(tenant_id: uuid.UUID, *, logged_in: bool) -> uuid.UUID:
    """Сотрудник отеля; `logged_in` — вошёл сейчас (новая сессия кабинета)."""
    async with platform_session_scope() as session:
        user = User(display_name="Сотрудник")
        session.add(user)
        await session.flush()
        session.add(
            TenantMembership(user_id=user.id, tenant_id=tenant_id, role_key=StaffRole.STAFF)
        )
        if logged_in:
            session.add(
                StaffSession(
                    user_id=user.id,
                    token_hash=uuid.uuid4().hex * 2,
                    expires_at=utc_now() + timedelta(days=1),
                )
            )
        return user.id


async def _ask_mentor(tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
    now = utc_now()
    with tenant_context(tenant_id):
        await mentor_api.append_messages(
            user_id,
            mentor_api.ConversationMode.ASK,
            [
                mentor_api.NewMentorMessage(
                    role=mentor_api.MessageRole.USER, text="Как встретить гостя?", created_at=now
                ),
                mentor_api.NewMentorMessage(
                    role=mentor_api.MessageRole.ASSISTANT, text="Статья 3.", created_at=now
                ),
            ],
        )


def _exam_assigned(monkeypatch: pytest.MonkeyPatch, assigned: bool = True) -> None:
    async def has_active_exam_assignment() -> bool:
        return assigned

    monkeypatch.setattr(mentor_api, "has_active_exam_assignment", has_active_exam_assignment)


def _freeze_after_noon(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сегодня отеля, после полудня и после уже записанных данных."""
    noon = datetime.combine(utc_now().astimezone(ZONE).date(), time(12, 1), tzinfo=ZONE)
    moment = max(utc_now(), noon.astimezone(UTC))
    monkeypatch.setattr(usage_pulse, "utc_now", lambda: moment)


async def _quiet(alerts: RecordingAlerts | None) -> int:
    return await send_quiet_day_alerts(
        alert_sender=alerts, alert_chat_id=ALERT_CHAT if alerts is not None else ""
    )


_NO_CHECKS = ReplyLanguageCount(checked=0, mismatched=0)


def _pulse(language: ReplyLanguageCount = _NO_CHECKS, **mentor: int) -> UsagePulse:
    activity = None
    if mentor:
        fields = dict.fromkeys(mentor_api.MentorActivity.model_fields, 0) | mentor
        activity = mentor_api.MentorActivity(**fields)
    return UsagePulse(
        staff=StaffLogins(logins=7, people=5), guests=2, mentor=activity, reply_language=language
    )


def test_lines_with_the_mentor_on() -> None:
    lines = pulse_lines(
        _pulse(turns=23, people=6, exams_started=5, exams_finished=3, exams_passed=2, ungraded=1)
    )
    assert lines == [
        "👥 Входы в кабинет: 7 (5 сотрудников). Гости подключились к чату: 2.",
        "🎓 «Наставник»: 23 вопроса от 6 сотрудников.",
        "📝 Экзамены: начато 5, закончено 3, сдано 2.",
        "⚠️ Не засчитано ответов на экзаменах: 1 — оценку не удалось разобрать.",
    ]


def test_empty_forms_and_the_mentor_off() -> None:
    empty = UsagePulse(
        staff=StaffLogins(logins=0, people=0),
        guests=0,
        mentor=mentor_api.MentorActivity(
            turns=0, people=0, exams_started=0, exams_finished=0, exams_passed=0, ungraded=0
        ),
        reply_language=_NO_CHECKS,
    )
    assert pulse_lines(empty) == [
        "👥 Входы в кабинет: 0. Гости подключились к чату: 0.",
        "🎓 «Наставник»: вопросов не было.",
        "📝 Экзамены: не начинали.",
    ]
    # «Наставник» выключен — о разделе, которого у отеля нет, копия молчит.
    assert pulse_lines(_pulse()) == [
        "👥 Входы в кабинет: 7 (5 сотрудников). Гости подключились к чату: 2."
    ]
    one = pulse_lines(_pulse(turns=1, people=1, exams_finished=1))
    assert one[0].startswith("👥 Входы в кабинет: 7 (5 сотрудников).")
    assert one[1:] == [
        "🎓 «Наставник»: 1 вопрос от 1 сотрудника.",
        "📝 Экзамены: начато 0, закончено 1, сдано 0.",
    ]


def test_reply_language_line_only_when_there_was_something_to_check() -> None:
    clean = pulse_lines(_pulse(ReplyLanguageCount(checked=57, mismatched=0)))
    assert clean[1:] == ["🌐 Ответы не на языке гостя (по алфавиту): 0 из 57."]
    wrong = pulse_lines(_pulse(ReplyLanguageCount(checked=57, mismatched=3), turns=1, people=1))
    assert wrong[1:3] == [
        "⚠️ Ответы не на языке гостя (по алфавиту): 3 из 57.",
        "🎓 «Наставник»: 1 вопрос от 1 сотрудника.",
    ]


async def test_founder_copy_carries_the_pulse_and_the_hotel_summary_does_not(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _configure(demo_tenant, hotel_chat="-100500")
    anna = await _staff(demo_tenant, logged_in=True)
    await _staff(demo_tenant, logged_in=False)
    await _ask_mentor(demo_tenant, anna)
    with tenant_context(demo_tenant):
        await bind_chat(4242, room="305")
        conversation = uuid.uuid4()
        await check_reply_language(conversation, "Where is the pool?", "The pool is on floor 2.")
        await check_reply_language(conversation, "Where is the gym?", "Тренажёрный зал на 3 этаже.")
    tomorrow_nine = datetime.combine(
        utc_now().astimezone(ZONE).date() + timedelta(days=1), time(9), tzinfo=ZONE
    )
    monkeypatch.setattr(daily_summary, "utc_now", lambda: tomorrow_nine.astimezone(UTC))

    sender, alerts = RecordingSender(), RecordingAlerts()
    assert (
        await send_daily_summaries(sender=sender, alert_sender=alerts, alert_chat_id=ALERT_CHAT)
        == 2
    )

    hotel_text, copy_text = sender.sent[0][1], alerts.sent[0]
    pulse = [
        "👥 Входы в кабинет: 1 (1 сотрудник). Гости подключились к чату: 1.",
        "⚠️ Ответы не на языке гостя (по алфавиту): 1 из 2.",
        "🎓 «Наставник»: 1 вопрос от 1 сотрудника.",
        "📝 Экзамены: не начинали.",
    ]
    assert f"{hotel_text}\n" + "\n".join(pulse) + "\nИИ за сутки отеля: $0.00" in copy_text
    assert "Входы в кабинет" not in hotel_text


async def test_quiet_day_alerts_once_after_noon(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _configure(demo_tenant)
    await _staff(demo_tenant, logged_in=False)
    _exam_assigned(monkeypatch)
    _freeze_after_noon(monkeypatch)

    alerts = RecordingAlerts()
    assert await _quiet(alerts) == 1
    assert await _quiet(alerts) == 0  # ключ суток записан (P-8)
    assert len(alerts.sent) == 1
    text = alerts.sent[0]
    assert text.startswith("🟡 [") and "ERR-OPS-022: тихий день" in text
    assert "Demo Hotel: сегодня до " in text
    assert f"tenant: {demo_tenant}" in text
    assert text.endswith("#err-ops-022")


async def test_no_alert_before_noon(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _configure(demo_tenant)
    _exam_assigned(monkeypatch)
    morning = datetime.combine(utc_now().astimezone(ZONE).date(), time(11, 59), tzinfo=ZONE)
    monkeypatch.setattr(usage_pulse, "utc_now", lambda: morning.astimezone(UTC))

    assert await _quiet(RecordingAlerts()) == 0


@pytest.mark.parametrize("activity", ["login", "mentor"])
async def test_any_activity_since_midnight_keeps_the_day_quiet_free(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch, activity: str
) -> None:
    await _configure(demo_tenant)
    user_id = await _staff(demo_tenant, logged_in=activity == "login")
    if activity == "mentor":
        await _ask_mentor(demo_tenant, user_id)
    _exam_assigned(monkeypatch)
    _freeze_after_noon(monkeypatch)

    assert await _quiet(RecordingAlerts()) == 0


async def test_no_alert_without_an_assignment_the_mentor_or_alerting(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _configure(demo_tenant)
    _freeze_after_noon(monkeypatch)
    _exam_assigned(monkeypatch, assigned=False)
    assert await _quiet(RecordingAlerts()) == 0

    _exam_assigned(monkeypatch)
    assert await _quiet(None) == 0
    await _configure(demo_tenant, mentor_enabled=False)
    assert await _quiet(RecordingAlerts()) == 0


async def test_failure_on_one_hotel_is_logged_and_does_not_stop_the_run(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_a, tenant_b = two_tenants
    await _configure(tenant_a)
    await _configure(tenant_b)
    _freeze_after_noon(monkeypatch)
    calls: list[uuid.UUID] = []

    async def flaky() -> bool:
        calls.append(current_tenant_id())
        if len(calls) == 1:
            raise RuntimeError("БД мигнула")
        return True

    monkeypatch.setattr(mentor_api, "has_active_exam_assignment", flaky)
    alerts = RecordingAlerts()
    assert await _quiet(alerts) == 1
    assert set(calls) == {tenant_a, tenant_b}
    # Ключ упавшего не записан — следующий прогон доходит до него снова, а второй отель
    # отсекает ключ суток раньше вопроса о назначении.
    assert await _quiet(alerts) == 1
    assert calls[2] == calls[0]
    assert len(calls) == 3
    assert len(alerts.sent) == 2
