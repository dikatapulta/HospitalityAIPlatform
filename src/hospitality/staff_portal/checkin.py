"""Данные и расчёты страницы заселения (spec 0033 §6, PR E серии #48).

Собирает контекст шаблона `checkin.html`: форма «комната → ночи кнопками →
гости → Заселить» и карточка Stay (QR ссылки привязки — живёт до выезда, им же
печатается листок гостю; код крупно, индикатор «гость подключился», действия).
Маршруты — `router.py`, JSON-действия карточки — `api_router.py`; здесь только
чтение через `guests_api` (P-5, R-5) и чистые расчёты времени/QR.

Время: `check_out_at` = дата заезда + ночи, 12:00 по поясу отеля → UTC (Q3,
правило spec 0033 §6; тот же дефолт, что CLI `tools/checkin`). Пояс — из
конфига тенанта, тенант без конфига — деградация на UTC (канон
`queue.hotel_midnight_utc`).

Просроченные проживания (spec 0043 §2.5): над формой — список «Не выехали в
срок» (`guests_api.list_overdue_stays`) с моментом автовыезда по запасу из
конфига тенанта; та же подпись — строкой на карточке просроченного Stay.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Any, Final
from urllib.parse import quote

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

# Кнопки формы (spec 0033 §6): ночи и гости; больше — полем «другое».
NIGHT_CHOICES: Final = (1, 2, 3, 7)
GUEST_CHOICES: Final = (1, 2, 3, 4)

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


def _parse_guests(guests: str, guests_custom: str) -> int:
    """Число гостей: «другое» побеждает кнопку, как у ночей."""
    custom = guests_custom.strip()
    if not custom:
        if guests not in {str(choice) for choice in GUEST_CHOICES}:
            raise CheckinFormError("Выберите число гостей кнопкой.")
        return int(guests)
    try:
        parsed = int(custom)
    except ValueError:
        raise CheckinFormError("Число гостей должно быть числом.") from None
    if not 1 <= parsed <= guests_api.MAX_GUESTS_PER_STAY:
        raise CheckinFormError(f"Число гостей — от 1 до {guests_api.MAX_GUESTS_PER_STAY}.")
    return parsed


def parse_checkin_form(
    room: str, nights: str, nights_custom: str, guests: str, guests_custom: str = ""
) -> CheckinForm:
    """Разбор полей формы; «другое» число побеждает кнопку-радио."""
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
    return CheckinForm(
        room_number=room,
        nights=parsed_nights,
        guests_count=_parse_guests(guests, guests_custom),
    )


@dataclass(frozen=True)
class HotelTiming:
    """Пояс отеля и запас автовыезда — одним чтением конфига на страницу."""

    zone: tzinfo
    # None — автовыезд у отеля выключен (или конфига нет: джоба такого
    # тенанта не обходит, spec 0043 §2.2).
    auto_checkout_grace: timedelta | None = None


async def hotel_timing(staff: StaffContext) -> HotelTiming:
    """Пояс и запас автовыезда; тенант без конфига — UTC без автовыезда, не отказ."""
    try:
        async with platform_session_scope() as session:
            config = await load_tenant_config(session, staff.tenant_id)
    except AppError as error:
        if error.code != TENANT_NOT_CONFIGURED_ERROR_CODE:
            raise
        logger.warning("staff.checkin_zone_utc_fallback", error_code=error.code)
        return HotelTiming(zone=UTC)
    return HotelTiming(zone=config.tzinfo, auto_checkout_grace=config.stay_auto_checkout_grace())


async def hotel_zone(staff: StaffContext) -> tzinfo:
    """Часовой пояс отеля; тенант без конфига — UTC (канон очереди), не отказ."""
    return (await hotel_timing(staff)).zone


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


def _day_and_time(moment: datetime, zone: tzinfo, now: datetime) -> str:
    """«в 14:00» сегодня по отелю, иначе «22.10 в 14:00»."""
    local = moment.astimezone(zone)
    clock = local.strftime("%H:%M")
    if local.date() == now.astimezone(zone).date():
        return f"в {clock}"
    return f"{local:%d.%m} в {clock}"


def auto_checkout_label(
    stay: guests_api.StayRead, timing: HotelTiming, now: datetime
) -> str | None:
    """Когда просроченный Stay закроется сам: «в 14:00»; None — автовыезда нет.

    Момент уже прошёл, а джоба ещё не дошла (интервал воркера) — «в ближайшие
    минуты», а не время в прошлом.
    """
    if timing.auto_checkout_grace is None:
        return None
    moment = stay.check_out_at + timing.auto_checkout_grace
    if moment <= now:
        return "в ближайшие минуты"
    return _day_and_time(moment, timing.zone, now)


def overdue_rows(
    stays: list[guests_api.StayRead], timing: HotelTiming, now: datetime
) -> list[dict[str, str | None]]:
    """Строки списка «Не выехали в срок» (spec 0043 §2.5)."""
    return [
        {
            "room_number": stay.room_number,
            "was_due": _day_and_time(stay.check_out_at, timing.zone, now),
            "auto_checkout": auto_checkout_label(stay, timing, now),
        }
        for stay in stays
    ]


def bind_link_url(tenant_slug: str, token: str) -> str:
    """Абсолютный URL ссылки привязки (в QR уходит полный адрес инсталляции)."""
    base = get_settings().public_base_url.rstrip("/")
    return f"{base}/w/{tenant_slug}/b/{token}"


def room_chat_url(tenant_slug: str, room_number: str) -> str:
    """Постоянный адрес веб-чата комнаты — содержимое комнатного QR (spec 0027 §3).

    Не секрет (ADR-008 §3): вход всё равно требует кода заселения. Комната —
    свободный ввод до 20 символов, поэтому кодируется целиком (`/`, пробел).
    Листок гостю печатает ссылку привязки (#354); этот адрес — для комнатных
    QR, которые печатаются пачкой на все номера (#226).
    """
    base = get_settings().public_base_url.rstrip("/")
    return f"{base}/g/{tenant_slug}/{quote(room_number, safe='')}"


def qr_svg(url: str) -> str:
    """Серверный SVG-QR (segno, spec 0033 §6) — инлайнится в карточку (CSP 'self').

    `light` — белая заливка ВНУТРИ картинки, вместе с зоной тишины: segno рисует
    модули жёстко чёрным (`stroke="#000"`), и в тёмной теме код без своей
    подложки не сканируется — то есть ломается заселение, а не оформление.
    Подложка живёт в SVG, а не в CSS, чтобы не зависеть ни от темы, ни от того,
    какие селекторы понимает браузер персонала; `fill` — атрибут, а не
    инлайн-стиль, поэтому CSP страницы (`style-src 'self'`) не при чём.

    `viewBox` вместо `width`/`height` (`omitsize`): без него CSS-ширина не
    масштабирует рисунок, а размер задаёт CSS — на экране по ширине карточки,
    на печатном листке в миллиметрах.
    """
    return segno.make(url, error="m").svg_inline(scale=4, light="#ffffff", omitsize=True)


async def stay_card(
    staff: StaffContext,
    stay: guests_api.StayRead,
    zone: tzinfo,
    *,
    access_code: str | None = None,
    bind_token: str | None = None,
    timing: HotelTiming | None = None,
) -> dict[str, Any]:
    """Контекст карточки Stay для шаблона.

    `access_code` и `bind_token` есть ТОЛЬКО сразу после заселения/выпуска —
    в БД лежат хэши, сервер их не восстановит; карточка по поиску комнаты
    показывает кнопки выпуска вместо значений. Тот же `qr_svg` уходит и на
    печатный листок гостю (spec 0033 §6).

    `timing` — для строки «выезд оформится сам …» у просроченного Stay (spec
    0043 §2.5); без него (карточка сразу после заселения) строки нет.
    """
    now = utc_now()
    overdue = stay.check_out_at <= now
    return {
        "stay_id": str(stay.id),
        "room_number": stay.room_number,
        "guests_count": stay.guests_count,
        "check_out_local": format_local(stay.check_out_at, zone),
        "overdue": overdue,
        "auto_checkout": (
            auto_checkout_label(stay, timing, now) if overdue and timing is not None else None
        ),
        "bindings_count": await guests_api.count_stay_sessions(stay.id),
        "access_code": guests_api.format_access_code(access_code) if access_code else None,
        "qr_svg": qr_svg(bind_link_url(staff.tenant_slug, bind_token)) if bind_token else None,
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
        "overdue": [],
    }
    timing = await hotel_timing(staff)
    if context["room_query"]:
        stay = await guests_api.find_active_stay(context["room_query"])
        if stay is not None:
            context["card"] = await stay_card(staff, stay, timing.zone, timing=timing)
            return context
        context["free_room"] = context["room_query"]
    now = utc_now()
    context["overdue"] = overdue_rows(await guests_api.list_overdue_stays(now=now), timing, now)
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
