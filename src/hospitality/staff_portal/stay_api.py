"""JSON-действия карточки заселения (spec 0033 §6, PR E): QR ссылки привязки,
перевыпуск кода, переселение и обмен комнатами (#237), продление, поздний выезд
(spec 0060), выезд и счётчик привязок. Зовёт `static/checkin.js`.

Канон — `queue_api.py`: CSRF-щит `require_json_same_origin` и `require_role`
напрямую; право — служба ресепшена любого уровня и менеджер (spec 0045 §2).
"""

from __future__ import annotations

import uuid
from typing import Final

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator

from hospitality.modules.guests import api as guests_api
from hospitality.platform import staff_auth
from hospitality.platform.models import StaffRole
from hospitality.shared.config import get_settings
from hospitality.shared.db import utc_now
from hospitality.shared.errors import VALIDATION_ERROR_CODE, AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.ratelimit import consume_rate_limit
from hospitality.staff_portal import checkin, turnovers
from hospitality.staff_portal.browser import require_json_same_origin

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8): превышен лимит выпуска
# bind-ссылок по (tenant, stay). Отказ CSRF-щита (ERR-AUTH-009) — `browser.py`.
ERR_BIND_LINK_RATE_LIMITED = "ERR-AUTH-010"

router = APIRouter(prefix="/{tenant_slug}/api", tags=["staff-portal"])

# Заселение и карточки Stay — служба ресепшена любого уровня и менеджер (spec 0045 §2).
_reception_role: Final = staff_auth.require_role(StaffRole.MANAGER, or_reception=True)


class MoveBody(BaseModel):
    """«Переселить» и обмен с гостем занятой комнаты (#237): новая комната; занятая —
    409 ERR-GUESTS-002, со «/» — 422 (#359: понятный текст раньше показывает `checkin.js`)."""

    room_number: str = Field(min_length=1, max_length=20, pattern=guests_api.ROOM_NUMBER_PATTERN)

    @field_validator("room_number")
    @classmethod
    def _strip_room(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("room number must not be blank")
        return stripped


class ExtendBody(BaseModel):
    """«Продлить»: на сколько ночей вперёд (расчёт локального времени — сервер)."""

    nights: int = Field(ge=1, le=checkin.MAX_NIGHTS)


class LateCheckoutBody(BaseModel):
    """«Поздний выезд» и «Вернуть выезд в 12:00»: час в день выезда по отелю
    (spec 0060 §2.2); день и пояс считает сервер."""

    hour: int = Field(ge=0, le=checkin.LATEST_CHECKOUT_HOUR)


class BindLinkView(BaseModel):
    """Свежая ссылка привязки: QR и URL показываются один раз (в БД — хэш)."""

    bind_url: str
    qr_svg: str


class ReissuedCodeView(BaseModel):
    """Новые код и QR — показываются один раз (в БД только хэши)."""

    access_code: str
    qr_svg: str


class StayActionView(BaseModel):
    """Итог действия над Stay для обновления карточки без перерисовки."""

    room_number: str
    check_out_local: str
    # Только у «Выселить»: номер ушёл на доску «Выезды и уборка» (spec 0046 §5),
    # карточка переводит туда. None — хаускипинга в кабинете нет.
    turnover_url: str | None = None
    # У продления и позднего выезда — признак позднего выезда (spec 0060 §3):
    # строка карточки «Поздний выезд до 16:00 — …»; None — выезд в расчётный час.
    late_checkout_note: str | None = None


class BindingsCountView(BaseModel):
    """Счётчик привязок Stay — поллинг индикатора «гость подключился» (3 с)."""

    count: int


async def _authorized_receptionist(request: Request) -> staff_auth.StaffContext:
    require_json_same_origin(request)
    return await _reception_role(request)


async def _enforce_bind_link_issue_limit(
    context: staff_auth.StaffContext, stay_id: uuid.UUID
) -> None:
    """Rate-limit выпуска bind-ссылок по (tenant, stay) (spec 0033 §9, канон 0023):
    человек столько не нажмёт — лимит ловит залипший скрипт."""
    settings = get_settings()
    limit = settings.guest_bind_link_issue_rate_limit_attempts
    if limit <= 0:
        return
    decision = await consume_rate_limit(
        "guest_bind_link_issue",
        f"{context.tenant_id}:{stay_id}",
        limit=limit,
        window_seconds=settings.guest_bind_link_issue_rate_limit_window_seconds,
    )
    if decision.available and not decision.allowed:
        logger.warning(
            "staff.bind_link_rate_limited",
            stay_id=str(stay_id),
            user_id=str(context.user_id),
            count=decision.count,
            limit=decision.limit,
        )
        raise AppError(
            code=ERR_BIND_LINK_RATE_LIMITED,
            message="Too many bind links for this stay — wait a minute and retry",
            status_code=429,
        )


@router.post("/stays/{stay_id}/bind-link", summary="Выпустить QR ссылки привязки (до выезда)")
async def issue_stay_bind_link(request: Request, stay_id: uuid.UUID) -> BindLinkView:
    """Ещё одна ссылка того же Stay: уже напечатанные не гаснут (spec 0033 §6)."""
    context = await _authorized_receptionist(request)
    await _enforce_bind_link_issue_limit(context, stay_id)
    token = await guests_api.issue_bind_link(stay_id)
    url = checkin.bind_link_url(context.tenant_slug, token)
    logger.info("staff.bind_link_issued", stay_id=str(stay_id), user_id=str(context.user_id))
    return BindLinkView(bind_url=url, qr_svg=checkin.qr_svg(url))


@router.post("/stays/{stay_id}/reissue-code", summary="Перевыпустить код и QR")
async def reissue_stay_code(request: Request, stay_id: uuid.UUID) -> ReissuedCodeView:
    """Ответ на «гость потерял листок»: перевыпуск кода гасит старый код и все
    QR этого Stay (#354), поэтому новый QR выпускается следом — карточка
    показывает новые код и QR, и листок готов к печати одним нажатием. Лимит —
    тот же, что у выпуска QR: каждый перевыпуск рождает ссылку."""
    context = await _authorized_receptionist(request)
    await _enforce_bind_link_issue_limit(context, stay_id)
    code = await guests_api.reissue_access_code(stay_id, acting_user=context.actor())
    token = await guests_api.issue_bind_link(stay_id)
    logger.info("staff.stay_code_reissued", stay_id=str(stay_id), user_id=str(context.user_id))
    return ReissuedCodeView(
        access_code=guests_api.format_access_code(code),
        qr_svg=checkin.qr_svg(checkin.bind_link_url(context.tenant_slug, token)),
    )


@router.post("/stays/{stay_id}/move", summary="Переселить гостя в другую комнату")
async def move_stay(request: Request, stay_id: uuid.UUID, body: MoveBody) -> StayActionView:
    context = await _authorized_receptionist(request)
    stay = await guests_api.move_stay(stay_id, body.room_number, acting_user=context.actor())
    logger.info(
        "staff.stay_moved",
        stay_id=str(stay_id),
        room_number=stay.room_number,
        user_id=str(context.user_id),
    )
    zone = await checkin.hotel_zone(context)
    return StayActionView(
        room_number=stay.room_number,
        check_out_local=checkin.format_local(stay.check_out_at, zone),
    )


@router.post("/stays/{stay_id}/swap", summary="Поменять гостей двух комнат местами")
async def swap_stay(request: Request, stay_id: uuid.UUID, body: MoveBody) -> StayActionView:
    """Обмен с гостем занятой комнаты (#237): `checkin.js` зовёт его после 409
    ERR-GUESTS-002 «Переселить» и подтверждения сотрудника. Тот гость выехал,
    пока сотрудник подтверждал, — ERR-GUESTS-001, ничего не меняется."""
    context = await _authorized_receptionist(request)
    stay = await guests_api.swap_stay_rooms(stay_id, body.room_number, acting_user=context.actor())
    logger.info(
        "staff.stay_rooms_swapped",
        stay_id=str(stay_id),
        room_number=stay.room_number,
        user_id=str(context.user_id),
    )
    zone = await checkin.hotel_zone(context)
    return StayActionView(
        room_number=stay.room_number,
        check_out_local=checkin.format_local(stay.check_out_at, zone),
    )


@router.post("/stays/{stay_id}/extend", summary="Продлить проживание на N ночей")
async def extend_stay(request: Request, stay_id: uuid.UUID, body: ExtendBody) -> StayActionView:
    """Новый срок считает сервер: +N к дате выезда (просроченному — от сегодня)
    в расчётный час отеля — поздний выезд снимается (spec 0060 §2.1); доступ
    гостя следует автоматически."""
    context = await _authorized_receptionist(request)
    stay = await checkin.get_active_stay_or_404(stay_id)
    timing = await checkin.hotel_timing(context)
    new_checkout = checkin.extended_checkout(stay.check_out_at, timing, body.nights, now=utc_now())
    updated = await guests_api.extend_stay(
        stay_id, new_checkout, nights=body.nights, acting_user=context.actor()
    )
    logger.info(
        "staff.stay_extended",
        stay_id=str(stay_id),
        nights=body.nights,
        user_id=str(context.user_id),
    )
    return _checkout_view(updated, timing)


@router.post("/stays/{stay_id}/late-checkout", summary="Поздний выезд: час выезда в день выезда")
async def set_late_checkout(
    request: Request, stay_id: uuid.UUID, body: LateCheckoutBody
) -> StayActionView:
    """Час в день выезда — поздний выезд или возврат к расчётному (spec 0060 §2.2).

    Раньше расчётного часа — 422 ERR-PLATFORM-002 (ранний выезд не делаем, §5);
    срок уже прошёл — ERR-GUESTS-004 домена. Чат гостя, автовыезд и доска
    следуют за сроком сами (§2.1).
    """
    context = await _authorized_receptionist(request)
    timing = await checkin.hotel_timing(context)
    if body.hour < timing.checkout_hour:
        raise AppError(
            code=VALIDATION_ERROR_CODE,
            message=f"Check-out hour must not be earlier than {timing.checkout_hour}:00",
            status_code=422,
        )
    stay = await checkin.get_active_stay_or_404(stay_id)
    new_checkout = checkin.checkout_on_departure_day(stay.check_out_at, timing, body.hour)
    updated = await guests_api.set_checkout_time(stay_id, new_checkout, acting_user=context.actor())
    logger.info(
        "staff.stay_checkout_time_set",
        stay_id=str(stay_id),
        hour=body.hour,
        user_id=str(context.user_id),
    )
    return _checkout_view(updated, timing)


def _checkout_view(stay: guests_api.StayRead, timing: checkin.HotelTiming) -> StayActionView:
    """Итог продления и позднего выезда: срок по поясу отеля и признак позднего выезда."""
    return StayActionView(
        room_number=stay.room_number,
        check_out_local=checkin.format_local(stay.check_out_at, timing.zone),
        late_checkout_note=checkin.late_checkout_note(stay, timing),
    )


@router.post("/stays/{stay_id}/checkout", summary="Выселить гостя (доступ гаснет)")
async def check_out_stay(request: Request, stay_id: uuid.UUID) -> StayActionView:
    """Выезд гостя; номер заодно уходит хаускипингу на доску (spec 0046 §5)."""
    context = await _authorized_receptionist(request)
    stay = await guests_api.check_out(stay_id, acting_user=context.actor())
    logger.info("staff.stay_checked_out", stay_id=str(stay_id), user_id=str(context.user_id))
    turnover_url = await turnovers.report_after_stay_checkout(context, stay.room_number)
    zone = await checkin.hotel_zone(context)
    return StayActionView(
        room_number=stay.room_number,
        check_out_local=checkin.format_local(stay.check_out_at, zone),
        turnover_url=turnover_url,
    )


@router.get("/stays/{stay_id}/bindings", summary="Счётчик привязок Stay (поллинг 3 с)")
async def stay_bindings(request: Request, stay_id: uuid.UUID) -> BindingsCountView:
    """GET ничего не меняет — CSRF-щит не нужен (контракт router.py), роль
    проверяется как у страницы."""
    await _reception_role(request)
    return BindingsCountView(count=await guests_api.count_stay_sessions(stay_id))
