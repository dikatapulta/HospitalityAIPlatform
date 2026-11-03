"""Карточка запроса столика в очереди кабинета (spec 0064 §2.3, §5, §6 PR 3; issue #702).

Запрос — заявка службы ресторана, но карточка у него своя: «🍽 Столик #12 ·
Терраса · 5 ноября, 19:30 · 2 гост. · № 512», имя и пожелания, кто и когда
ответил и три ответа — «Подтвердить», «Предложить другое время», «Мест нет».
Любой ответ сам берёт и закрывает заявку, поэтому «Взять», «Готово»,
«Вернуть в очередь» и «Отменить» общей карточки у брони нет (ревью PR #711,
Н-1): взятая и забытая бронь ушла бы из лестницы невзятых. «Передать» есть —
ошиблись службой. Час брони прошёл без ответа — «Закрыть запрос»: ответить уже
нельзя, а заявка иначе висела бы в очереди вечно. Данные — `table_booking.api`.
"""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from typing import Any, Final

from hospitality.modules.requests import api as requests_api
from hospitality.modules.table_booking import api as table_booking_api
from hospitality.platform.config import TenantConfig

Stage = table_booking_api.BookingStage
Answer = table_booking_api.BookingAnswer

# Названия ресторанов в кабинете — по-русски (язык кабинета, FOUNDATION §7).
_STAFF_LANGUAGE: Final = "ru"
_STAGE_LABELS: Final[dict[Stage, str]] = {
    Stage.WAITING: "запрос",
    Stage.CONFIRMED: "подтверждён",
    Stage.OTHER_TIME: "другое время",
    Stage.NO_SEATS: "мест нет",
    Stage.CANCELLED: "гость отменил",
    Stage.NO_ANSWER: "не ответили",
}
OVERDUE_LABEL: Final = "просрочен"
ANSWER_HINT: Final = (
    "Сначала сверьте с книгой броней ресторана. "
    "После „Подтвердить“ гость получит „Столик забронирован“."
)
LATE_HINT: Final = (
    "Время брони прошло — ресторан не ответил. "
    "После „Закрыть запрос“ гость получит сообщение об этом."
)


def with_booking(
    card: dict[str, Any],
    booking: table_booking_api.StaffBookingRead | None,
    request: requests_api.ServiceRequestRead,
    config: TenantConfig | None,
) -> dict[str, Any]:
    """Запросу столика — своя карточка: шаблон по `booking` рисует её кнопки вместо общих."""
    if booking is None:
        return card | {"booking": None}
    fields = booking_card(booking, request, config, is_overdue=card["is_overdue"])
    return card | {
        "booking": fields,
        "badge_label": fields["badge_label"],
        "badge_tone": fields["badge_tone"],
    }


def booking_card(
    booking: table_booking_api.StaffBookingRead,
    request: requests_api.ServiceRequestRead,
    config: TenantConfig | None,
    *,
    is_overdue: bool,
) -> dict[str, Any]:
    """Поля карточки запроса столика поверх общей карточки очереди (`queue._card`)."""
    zone: tzinfo = config.tzinfo if config is not None else UTC
    number = f"#{booking.number}" if booking.number is not None else ""
    title = " · ".join(
        part
        for part in (
            f"🍽 Столик {number}".rstrip(),
            booking.restaurant_names.text_for(_STAFF_LANGUAGE),
            f"{booking.day_label}, {booking.local_time}",
            f"{booking.party_size} гост.",
            f"№ {booking.room_number}" if booking.room_number else "",
        )
        if part
    )
    waiting = booking.stage is Stage.WAITING
    late = booking.can_close_unanswered
    return {
        "title": title,
        "badge_label": OVERDUE_LABEL if waiting and is_overdue else _STAGE_LABELS[booking.stage],
        "badge_tone": "overdue" if (waiting and is_overdue) or late else request.status.value,
        # «На имя: …» и «Пожелания: …» — `details` заявки, по строке (§3).
        "lines": (request.details or "").splitlines(),
        "steps": _steps(booking, request, zone),
        "can_answer": booking.can_answer,
        "answer_hint": ANSWER_HINT,
        "other_time_slots": booking.other_time_slots,
        "can_close": late,
        "late_hint": LATE_HINT if late else None,
    }


def _steps(
    booking: table_booking_api.StaffBookingRead,
    request: requests_api.ServiceRequestRead,
    zone: tzinfo,
) -> list[str]:
    """Кто и когда ответил: «Подтверждено: Айгерим, 14:05» — время отеля."""
    steps = []
    if booking.answered_at is not None:
        if booking.answer is Answer.CONFIRMED:
            label = "Подтверждено"
        elif booking.answer is Answer.OTHER_TIME:
            label = f"Предложено {booking.proposed_time}"
        else:
            label = "Мест нет"
        steps.append(_step(label, booking.answered_by_display_name, booking.answered_at, zone))
    elif request.closed_at is not None and request.status is requests_api.RequestStatus.DONE:
        steps.append(
            _step("Закрыт без ответа", request.closed_by_display_name, request.closed_at, zone)
        )
    if booking.cancelled_by_guest_at is not None:
        steps.append(_step("Гость отменил", None, booking.cancelled_by_guest_at, zone))
    return steps


def _step(label: str, name: str | None, moment: datetime, zone: tzinfo) -> str:
    when = moment.astimezone(zone).strftime("%H:%M")
    return f"{label}: {name}, {when}" if name else f"{label}: {when}"
