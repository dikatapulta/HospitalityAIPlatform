"""Запрос посетителя витрины → заявка службе (spec 0069 §2.4; PR (в), #805) — канон
`table_booking/bookings.py`.

Запрос — строка модуля с контактами плюс заявка в службе продаж (группа) или
бронирования (гость; службы нет — тоже продажи): номер `#N`, очередь, сигнал, пуш
и лестница невзятой заявки — у заявки. Порядок записи — Р-3 spec 0038: строка с
`request_id = uuid4()` → `requests.create_request(request_id=…)`, идемпотентный
по id → отметка `request_created`. Сбой между шагами — строка без заявки: её не
видит никто, повтор «Отправить» тем же ключом доводит дело, брошенную удаляет
ретеншн через сутки.

Это запрос, а не бронь (P-9, NG-2, NG-3): наличие, цену и бронь подтверждает
человек в Opera. В тексте заявки контактов нет — они в модуле со сроком 365 дней
(§3); суть заявки — вид запроса, даты, гостей, компания, номера или вид.

Каждая функция вызывается внутри `tenant_context` (его ставит канал по slug).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Final

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from hospitality.modules.requests import api as requests_api
from hospitality.modules.showroom.request_models import ShowroomRequest, ShowroomRequestKind
from hospitality.modules.showroom.request_scene import SceneFacts, load_scene_facts
from hospitality.modules.showroom.request_schemas import (
    MAX_DAYS_AHEAD,
    ShowroomRequestCreate,
    ShowroomRequestPlaced,
    ShowroomRequestRead,
)
from hospitality.modules.showroom.service import ERR_SHOWROOM_DISABLED
from hospitality.platform.config import LocalizedText, TenantConfig, load_tenant_config
from hospitality.platform.showroom_config import ShowroomConfig
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.humanize import month_day_ru
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_showroom_request
from hospitality.shared.pii import mask_payment_card_numbers
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8); 001 — у сцены и доступа (`service.py`).
ERR_SHOWROOM_REQUEST_INVALID: Final = "ERR-SHOWROOM-004"

_CLIENT_KEY_CONSTRAINT: Final = "uq_showroom_requests_client_key"
# Названия в заявке и карточке — по-русски (язык кабинета, FOUNDATION §7).
_STAFF_LANGUAGE: Final = "ru"


def invalid(message: str) -> AppError:
    return AppError(code=ERR_SHOWROOM_REQUEST_INVALID, message=message, status_code=422)


def disabled(message: str) -> AppError:
    return AppError(code=ERR_SHOWROOM_DISABLED, message=message, status_code=404)


async def create_request(
    data: ShowroomRequestCreate, *, consent_version: str
) -> ShowroomRequestPlaced:
    """«Отправить запрос»: проверки, строка с контактами, заявка в службу.

    Повтор тем же `client_key` — тот же запрос (`duplicate=True`), и заявка
    доводится, если в прошлый раз не успела. Порядок отказов: витрина выключена,
    сцены или службы нет (001); даты, гости, номер или вид вне правил (004).
    """
    existing = await _find_by_client_key(data.client_key)
    if existing is not None:
        return ShowroomRequestPlaced(request=await _ensure_request(existing), duplicate=True)
    showroom, config = await _showroom()
    facts = await load_scene_facts()
    if facts is None:
        raise disabled("Showroom has no scene")
    _check(data, showroom, facts, today=_hotel_today(config))
    category_id = await _category_id(_category_key(data.kind, showroom))

    now = utc_now()
    row = ShowroomRequest(
        id=uuid.uuid4(),
        tenant_id=current_tenant_id(),
        request_id=uuid.uuid4(),
        request_created=False,
        client_key=data.client_key,
        kind=data.kind,
        company=data.company,
        contact_name=data.contact_name,
        phone=data.phone,
        email=data.email,
        guests_count=data.guests_count,
        check_in=data.check_in,
        check_out=data.check_out,
        room_numbers=list(data.room_numbers),
        wish_view_key=data.wish_view_key,
        wish_view_names=_names(facts.views, data.wish_view_key),
        wish_type_key=data.wish_type_key,
        wish_type_names=_names(facts.room_types, data.wish_type_key),
        # Свободный текст — номер карты маскируется (NG-3), как у пожеланий столика.
        wishes=mask_payment_card_numbers(data.wishes) if data.wishes else None,
        language=data.language,
        consent_version=consent_version,
        consent_at=now,
        created_at=now,
    )
    try:
        async with session_scope() as session:
            session.add(row)
    except IntegrityError as error:
        # Двойное «Отправить» в одну секунду: второй проигрывает ключу и отдаёт запрос первого.
        if _CLIENT_KEY_CONSTRAINT not in str(error):
            raise
        winner = await _find_by_client_key(data.client_key)
        if winner is None:  # pragma: no cover — нарушение ключа без строки невозможно
            raise
        return ShowroomRequestPlaced(request=await _ensure_request(winner), duplicate=True)
    placed = await _ensure_request(row, category_id=category_id)
    record_showroom_request(row.kind.value)
    logger.info(
        "showroom_request_created",
        showroom_request_id=str(row.id),
        request_id=str(row.request_id),
        kind=row.kind.value,
        guests_count=row.guests_count,
        rooms=len(row.room_numbers),
    )
    return ShowroomRequestPlaced(request=placed, duplicate=False)


def _check(
    data: ShowroomRequestCreate, showroom: ShowroomConfig, facts: SceneFacts, *, today: date
) -> None:
    """Правила формы, которых схема не знает (§2.4): даты отеля, гости брони, сцена."""
    if data.check_in < today:
        raise invalid("Check-in is in the past")
    if data.check_out <= data.check_in:
        raise invalid("Check-out must be after check-in")
    if data.check_out > today + timedelta(days=MAX_DAYS_AHEAD):
        raise invalid(f"Dates must be within {MAX_DAYS_AHEAD} days")
    if data.kind is ShowroomRequestKind.BOOKING and data.guests_count > showroom.booking_max_guests:
        raise invalid(f"A booking request is for at most {showroom.booking_max_guests} guests")
    unknown = [number for number in data.room_numbers if number not in facts.room_numbers]
    if unknown:
        raise invalid(f"Rooms are not in the scene: {', '.join(unknown[:5])}")
    if data.wish_view_key is not None and data.wish_view_key not in facts.views:
        raise invalid(f"View {data.wish_view_key!r} is not in the scene")
    if data.wish_type_key is not None and data.wish_type_key not in facts.room_types:
        raise invalid(f"Room type {data.wish_type_key!r} is not in the scene")


def _names(names: Mapping[str, LocalizedText], key: str | None) -> dict[str, str] | None:
    found = names.get(key) if key is not None else None
    return found.as_json() if found is not None else None


async def _showroom() -> tuple[ShowroomConfig, TenantConfig]:
    """Витрина отеля; выключена или настроек нет — 404 001 (§4)."""
    async with session_scope() as session:
        config = await load_tenant_config(session, current_tenant_id())
    showroom = config.showroom
    if showroom is None or not showroom.enabled:
        raise disabled("Showroom is disabled")
    return showroom, config


def _hotel_today(config: TenantConfig) -> date:
    return utc_now().astimezone(config.tzinfo).date()


def _category_key(kind: ShowroomRequestKind, showroom: ShowroomConfig) -> str:
    """Группа — в продажи; гость — в бронирование, а без неё — тоже в продажи (§2.4)."""
    if kind is ShowroomRequestKind.BOOKING and showroom.booking_category_key:
        return showroom.booking_category_key
    return showroom.sales_category_key


async def _category_id(category_key: str) -> uuid.UUID:
    for category in await requests_api.list_categories():
        if category.key == category_key:
            return category.id
    # Онбординг сверяет службы витрины с категориями отеля; без службы запрос некуда отдать.
    logger.error("showroom_category_missing", category_key=category_key)
    raise disabled(f"Showroom category {category_key!r} does not exist")


async def _find_by_client_key(client_key: str) -> ShowroomRequest | None:
    async with session_scope() as session:
        found: ShowroomRequest | None = await session.scalar(
            select(ShowroomRequest).where(ShowroomRequest.client_key == client_key)
        )
    return found


async def _ensure_request(
    row: ShowroomRequest, *, category_id: uuid.UUID | None = None
) -> ShowroomRequestRead:
    """Заявка запроса (идемпотентно по `request_id`) и отметка «заявка есть»."""
    if not row.request_created:
        if category_id is None:
            showroom, _ = await _showroom()
            category_id = await _category_id(_category_key(row.kind, showroom))
        await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category_id,
                origin=requests_api.ServiceRequestOrigin.SHOWROOM_FORM,
                summary=request_summary(row),
                details=request_details(row),
                guest_language=row.language,
            ),
            request_id=row.request_id,
        )
        async with session_scope() as session:
            await session.execute(
                update(ShowroomRequest)
                .where(ShowroomRequest.id == row.id)
                .values(request_created=True)
            )
        row.request_created = True
    return ShowroomRequestRead(
        id=row.id, request_id=row.request_id, kind=row.kind, created_at=row.created_at
    )


def date_range_ru(check_in: date, check_out: date) -> str:
    """«12–15 марта», «28 марта – 2 апреля»; год — только когда он не один."""
    if check_in.year != check_out.year:
        first, last = month_day_ru(check_in), month_day_ru(check_out)
        return f"{first} {check_in.year} – {last} {check_out.year}"
    if check_in.month == check_out.month:
        return f"{check_in.day}–{month_day_ru(check_out)}"
    return f"{month_day_ru(check_in)} – {month_day_ru(check_out)}"


def wish_ru(
    room_numbers: list[str],
    view_names: LocalizedText | None,
    type_names: LocalizedText | None,
) -> str | None:
    """«Номера: 1901, 1903» — выбранные на модели; иначе «вид «Горы», тип «Делюкс»»."""
    if room_numbers:
        return ", ".join(room_numbers)
    parts = [
        f"{label} «{names.text_for(_STAFF_LANGUAGE)}»"
        for label, names in (("вид", view_names), ("тип", type_names))
        if names is not None
    ]
    return ", ".join(parts) or None


def _localized(names: dict[str, str] | None) -> LocalizedText | None:
    return LocalizedText.model_validate(names) if names else None


def request_summary(row: ShowroomRequest) -> str:
    """Суть заявки для службы, по-русски; срок — как у любого текста заявки (spec 0032)."""
    dates = date_range_ru(row.check_in, row.check_out)
    guests = f"{row.guests_count} гост."
    if row.kind is ShowroomRequestKind.GROUP:
        return " · ".join(part for part in ("Групповой запрос", row.company, guests, dates) if part)
    wish = wish_ru(
        row.room_numbers, _localized(row.wish_view_names), _localized(row.wish_type_names)
    )
    return " · ".join(part for part in ("Запрос на бронирование", dates, guests, wish) if part)


def request_details(row: ShowroomRequest) -> str | None:
    """Номера или желаемый вид — строкой заявки; контактов и пожеланий здесь нет (§2.4)."""
    if row.room_numbers:
        return f"Номера: {', '.join(row.room_numbers)}"
    wish = wish_ru([], _localized(row.wish_view_names), _localized(row.wish_type_names))
    return f"Желание: {wish}" if wish else None
