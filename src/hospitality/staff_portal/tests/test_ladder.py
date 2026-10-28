"""Лестница невзятой заявки чистой функцией (spec 0045 §4, §10 «PR 2»):
границы `d` и `2d`, три «сразу на третью» и выключенный срок."""

from __future__ import annotations

from datetime import timedelta

import pytest

from hospitality.staff_portal.ladder import LadderStep, ladder_step, step_by_time

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
