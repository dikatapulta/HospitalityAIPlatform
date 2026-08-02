"""Данные и расчёты страницы заселения (spec 0033 §6, PR E серии #48).

Собирает контекст шаблона `checkin.html`: форма «комната → ночи кнопками →
гости → Заселить» и карточка Stay (QR одноразовой bind-ссылки, код крупно,
индикатор «гость подключился», действия). Маршруты — `router.py`, JSON-действия
карточки — `api_router.py`; здесь только чтение через `guests_api` (P-5, R-5)
и чистые расчёты времени/QR.

Время: `check_out_at` = дата заезда + ночи, 12:00 по поясу отеля → UTC (Q3,
правило spec 0033 §6; тот же дефолт, что CLI `tools/checkin`). Пояс — из
конфига тенанта, тенант без конфига — деградация на UTC (канон
`queue._hotel_midnight_utc`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Any, Final

import segno

from hospitality.modules.guests import api as guests_api
from hospitality.platform.config import TENANT_NOT_CONFIGURED_ERROR_CODE, load_tenant_config
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Час выезда, локальное время отеля (spec 0033 §6, Q3) — как в CLI.
CHECKOUT_HOUR: Final = 12

# Кнопки формы (spec 0033 §6): ночи и гости; «3+» хранится как 3.
NIGHT_CHOICES: Final = (1, 2, 3, 7)
GUEST_CHOICES: Final = (1, 2, 3)

# Потолок «другого» числа ночей: опечатка не должна заселить на годы.
MAX_NIGHTS: Final = 90


class CheckinFormError(ValueError):
    """Ошибка ввода формы заселения: текст показывается пользователю."""


@dataclass(frozen=True)
class CheckinForm:
    """Разобранная форма «Заселить»."""

    room_number: str
    nights: int
    guests_count: int


def parse_checkin_form(room: str, nights: str, nights_custom: str, guests: str) -> CheckinForm:
    """Разбор полей формы; «другое» число ночей побеждает кнопку-радио."""
    room = room.strip()
    if not room or len(room) > 20:
        raise CheckinFormError("Укажите номер комнаты (до 20 символов).")
    raw_nights = nights_custom.strip() or nights.strip()
    try:
        parsed_nights = int(raw_nights)
    except ValueError:
        raise CheckinFormError("Число ночей должно быть числом.") from None
    if not 1 <= parsed_nights <= MAX_NIGHTS:
        raise CheckinFormError(f"Число ночей — от 1 до {MAX_NIGHTS}.")
    if guests not in {str(choice) for choice in GUEST_CHOICES}:
        raise CheckinFormError("Выберите число гостей кнопкой.")
    return CheckinForm(room_number=room, nights=parsed_nights, guests_count=int(guests))


async def hotel_zone(staff: StaffContext) -> tzinfo:
    """Часовой пояс отеля; тенант без конфига — UTC (канон очереди), не отказ."""
    zone: tzinfo = UTC
    try:
        async with platform_session_scope() as session:
            config = await load_tenant_config(session, staff.tenant_id)
        zone = config.tzinfo
    except AppError as error:
        if error.code != TENANT_NOT_CONFIGURED_ERROR_CODE:
            raise
        logger.warning("staff.checkin_zone_utc_fallback", error_code=error.code)
    return zone


def checkout_at_utc(zone: tzinfo, nights: int, *, now: datetime) -> datetime:
    """Момент выезда в UTC: сегодня (по отелю) + ночи, 12:00 локального времени."""
    local_today = now.astimezone(zone).date()
    local = datetime.combine(local_today + timedelta(days=nights), time(CHECKOUT_HOUR), tzinfo=zone)
    return local.astimezone(UTC)


def extended_checkout(current: datetime, zone: tzinfo, nights: int, *, now: datetime) -> datetime:
    """Новый срок «+N ночей»: к дате выезда, просроченному Stay — от сегодня.

    Локальное время выезда сохраняется (обычно 12:00; выставленное CLI вручную
    не перетирается). База — max(дата выезда, сегодня): «+1 ночь» просроченного
    на три дня Stay значит «до завтра», а не «до позавчера».
    """
    local = current.astimezone(zone)
    base = max(local.date(), now.astimezone(zone).date())
    return datetime.combine(base + timedelta(days=nights), local.timetz()).astimezone(UTC)


def format_local(moment: datetime, zone: tzinfo) -> str:
    """Момент для показа персоналу — локальное время отеля."""
    return moment.astimezone(zone).strftime("%d.%m.%Y %H:%M")


def bind_link_url(tenant_slug: str, token: str) -> str:
    """Абсолютный URL bind-ссылки (в QR уходит полный адрес инсталляции)."""
    base = get_settings().public_base_url.rstrip("/")
    return f"{base}/w/{tenant_slug}/b/{token}"


def qr_svg(url: str) -> str:
    """Серверный SVG-QR (segno, spec 0033 §6) — инлайнится в карточку (CSP 'self').

    `light` — белая заливка ВНУТРИ картинки, вместе с зоной тишины: segno рисует
    модули жёстко чёрным (`stroke="#000"`), и в тёмной теме код без своей
    подложки не сканируется — то есть ломается заселение, а не оформление.
    Подложка живёт в SVG, а не в CSS, чтобы не зависеть ни от темы, ни от того,
    какие селекторы понимает браузер персонала; `fill` — атрибут, а не
    инлайн-стиль, поэтому CSP страницы (`style-src 'self'`) не при чём.
    """
    return segno.make(url, error="m").svg_inline(scale=4, light="#ffffff")


async def stay_card(
    staff: StaffContext,
    stay: guests_api.StayRead,
    zone: tzinfo,
    *,
    access_code: str | None = None,
    bind_token: str | None = None,
) -> dict[str, Any]:
    """Контекст карточки Stay для шаблона.

    `access_code` и `bind_token` есть ТОЛЬКО сразу после заселения/выпуска —
    в БД лежат хэши, сервер их не восстановит; карточка по поиску комнаты
    показывает кнопки перевыпуска вместо значений.
    """
    return {
        "stay_id": str(stay.id),
        "room_number": stay.room_number,
        "guests_count": stay.guests_count,
        "check_out_local": format_local(stay.check_out_at, zone),
        "overdue": stay.check_out_at <= utc_now(),
        "bindings_count": await guests_api.count_stay_sessions(stay.id),
        "access_code": guests_api.format_access_code(access_code) if access_code else None,
        "qr_svg": qr_svg(bind_link_url(staff.tenant_slug, bind_token)) if bind_token else None,
        "bind_ttl_seconds": guests_api.BIND_LINK_TTL_SECONDS,
    }


async def build_checkin_context(
    staff: StaffContext, room_query: str | None, *, flash: str | None = None
) -> dict[str, Any]:
    """Контекст страницы: карточка занятой комнаты или форма заселения.

    Вызывается внутри контекста тенанта запроса (звено TenantResolver) —
    `guests_api` читает под RLS. `?room=` без активного Stay — та же форма
    с подсказкой «свободна» (поиск и заселение — один ввод, spec 0033 §6).
    """
    context: dict[str, Any] = {
        "display_name": staff.display_name,
        "tenant_name": staff.tenant_name,
        "tenant_slug": staff.tenant_slug,
        "night_choices": NIGHT_CHOICES,
        "guest_choices": GUEST_CHOICES,
        "room_query": (room_query or "").strip(),
        "card": None,
        "free_room": None,
        "flash": flash,
        "error": None,
        "actions_endpoint": f"/staff/{staff.tenant_slug}/api/stays",
    }
    if context["room_query"]:
        stay = await guests_api.find_active_stay(context["room_query"])
        if stay is not None:
            context["card"] = await stay_card(staff, stay, await hotel_zone(staff))
        else:
            context["free_room"] = context["room_query"]
    return context


async def get_active_stay_or_404(stay_id: uuid.UUID) -> guests_api.StayRead:
    """Stay для JSON-действий карточки; закрытый/чужой — 404 кодом домена."""
    stay = await guests_api.get_active_stay(stay_id)
    if stay is None:
        raise AppError(
            code=guests_api.ERR_GUESTS_STAY_NOT_FOUND,
            message=f"Active stay {stay_id} does not exist",
            status_code=404,
        )
    return stay
