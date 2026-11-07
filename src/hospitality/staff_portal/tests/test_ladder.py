"""Лестница невзятой заявки чистой функцией (spec 0045 §4, §10 «PR 2»):
границы `d` и `2d`, три «сразу на третью» и выключенный срок."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hospitality.staff_portal.ladder import LadderStep, guest_raised, ladder_step, step_by_time

D = timedelta(minutes=30)
TICK = timedelta(seconds=1)


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(0), LadderStep.SERVICE),
        (D - TICK, LadderStep.SERVICE),
        (D, LadderStep.OVERDUE),
        (2 * D - TICK, LadderStep.OVERDUE),
        (2 * D, LadderStep.RECEPTION),
        (10 * D, LadderStep.RECEPTION),
    ],
)
def test_steps_by_time_on_both_boundaries(age: timedelta, expected: LadderStep) -> None:
    """Границы включительны, как у метки «просрочена»."""
    assert step_by_time(age, D) is expected
    assert (
        ladder_step(age=age, delay=D, is_urgent=False, is_escalation=False, service_staffed=True)
        is expected
    )


@pytest.mark.parametrize(
    ("is_urgent", "is_escalation", "service_staffed"),
    [(True, False, True), (False, True, True), (False, False, False)],
    ids=["urgent", "escalation", "service-without-staff"],
)
def test_three_cases_go_straight_to_reception(
    is_urgent: bool, is_escalation: bool, service_staffed: bool
) -> None:
    """С первой секунды и даже при выключенном сроке."""
    for delay in (D, None):
        assert (
            ladder_step(
                age=timedelta(0),
                delay=delay,
                is_urgent=is_urgent,
                is_escalation=is_escalation,
                service_staffed=service_staffed,
            )
            is LadderStep.RECEPTION
        )


def test_switched_off_deadline_has_no_time_steps() -> None:
    """Срок `None` (`request_reminders --off`): ни просрочки, ни подъёма по времени."""
    assert step_by_time(timedelta(days=3), None) is LadderStep.SERVICE
    assert (
        ladder_step(
            age=timedelta(days=3),
            delay=None,
            is_urgent=False,
            is_escalation=False,
            service_staffed=True,
        )
        is LadderStep.SERVICE
    )


# ---------------------------------------------------------------------------
# Гость напоминает (spec 0070 §5)
# ---------------------------------------------------------------------------

CREATED = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
STALLED = timedelta(hours=2)


def _raised(
    count: int,
    reminded_after: timedelta | None,
    *,
    claimed_after: timedelta | None = None,
    raise_after: int | None = 2,
    delay: timedelta | None = D,
    stalled_delay: timedelta | None = None,
) -> bool:
    return guest_raised(
        reminder_count=count,
        reminded_at=None if reminded_after is None else CREATED + reminded_after,
        created_at=CREATED,
        claimed_at=None if claimed_after is None else CREATED + claimed_after,
        is_claimed=claimed_after is not None,
        raise_after=raise_after,
        delay=delay,
        stalled_delay=stalled_delay,
    )


def test_guest_raises_from_threshold_or_first_when_overdue() -> None:
    """Невзятая: со второго напоминания; уже просроченная (через `d`) — с первого."""
    assert _raised(0, None) is False
    assert _raised(1, D - TICK) is False
    assert _raised(1, D) is True
    assert _raised(2, TICK) is True
    assert _raised(2, TICK, raise_after=3) is False
    assert _raised(1, 10 * D, delay=None) is False  # срок выключен — только по числу


def test_switched_off_threshold_never_raises() -> None:
    assert _raised(9, 10 * D, raise_after=None) is False


def test_claimed_raises_only_by_reminder_after_the_claim() -> None:
    """Поднятая и тут же взятая не зовёт заново; новое напоминание после взятия — зовёт.
    Просроченная взятая — по сроку «долго в работе» от взятия; выключен — по числу."""
    claim = timedelta(minutes=10)
    assert _raised(2, claim - TICK, claimed_after=claim) is False
    assert _raised(3, claim + TICK, claimed_after=claim) is True
    assert _raised(1, claim + STALLED - TICK, claimed_after=claim, stalled_delay=STALLED) is False
    assert _raised(1, claim + STALLED, claimed_after=claim, stalled_delay=STALLED) is True
    assert _raised(1, claim + 10 * STALLED, claimed_after=claim) is False


def test_guest_raised_goes_straight_to_reception() -> None:
    step = ladder_step(
        age=timedelta(0),
        delay=D,
        is_urgent=False,
        is_escalation=False,
        service_staffed=True,
        is_guest_raised=True,
    )
    assert step is LadderStep.RECEPTION
