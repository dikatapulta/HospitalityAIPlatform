"""3D-витрина демо-отеля (spec 0069 §6 PR а): вымышленное здание и настройки для staging.

Здание выдумано — не планировка пилота (она — данные отеля, `*.local`, загружает
основатель кабинетом): 16 этажей, длинная ось с запада на восток, город на севере,
горы на юге. Номера — `{этаж}{NN}`, по 10 на длинных фасадах и угловые на торцах.
Форма — как у прототипа витрины (#816): скруглённые торцы, фасад «гармошкой», подиум,
корона; рядом — вымышленные конгресс-холл и проспект.
Службы продаж демо-отеля: `sales` и `reservations` — адресаты витрины,
`banquet-sales` — пока без маршрута витрины (банкетные залы в 3D не решены, #799):
ручная заявка в неё работает, витрина в неё не шлёт.

Идемпотентно, как весь сид: категория с занятым ключом пропускается, настройки
витрины дописываются, только если ключа `showroom` в конфиге нет (правка руками
переживает деплой), сцена — только если её нет (загруженную менеджером не трогает).
Код доступа сид не ставит: страница посетителя на staging открывается по коду,
который задаёт основатель командой `showroom_access_code`.
"""

from __future__ import annotations

import uuid
from typing import Final

from hospitality.modules.requests.api import (
    ERR_REQUESTS_CATEGORY_KEY_TAKEN,
    RequestCategoryCreate,
    create_category,
)
from hospitality.modules.showroom import api as showroom_api
from hospitality.platform.config import LocalizedText, TenantConfig, mutate_tenant_config
from hospitality.platform.showroom_config import ShowroomConfig
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import tenant_context

logger = get_logger(module=__name__)

DEMO_SHOWROOM_CATEGORIES: Final = (
    RequestCategoryCreate(key="sales", name="Отдел продаж"),
    RequestCategoryCreate(key="reservations", name="Отдел бронирования"),
    RequestCategoryCreate(key="banquet-sales", name="Отдел продаж банкетных залов"),
)
DEMO_SHOWROOM: Final = ShowroomConfig(
    enabled=True, sales_category_key="sales", booking_category_key="reservations"
)
_FLOORS_TOTAL: Final = 16
_ROOM_FLOORS: Final = range(3, 16)


def _text(en: str, ru: str, kk: str) -> LocalizedText:
    return LocalizedText(en=en, ru=ru, kk=kk)


_SIDES_ALONG_FLOOR: Final[tuple[showroom_api.SideKey, ...]] = (
    "west",
    *(("north",) * 10),
    "east",
    *(("south",) * 10),
)


def _room_type(floor: int, side: str) -> str:
    if side in ("east", "west"):
        return "suite" if floor >= 13 else "deluxe"
    return "standard"


def build_demo_scene() -> showroom_api.SceneDocument:
    """Вымышленное здание демо-отеля."""
    floors: list[showroom_api.Floor] = [
        showroom_api.Floor(number=1, kind="lobby", label=_text("Lobby", "Лобби", "Лобби")),
        showroom_api.Floor(
            number=2,
            kind="restaurant",
            label=_text("Restaurant and hall", "Ресторан и зал", "Мейрамхана және зал"),
            spaces=(
                showroom_api.Space(
                    key="alatau",
                    kind="hall",
                    names=_text("Alatau hall", "Зал «Алатау»", "«Алатау» залы"),
                    area_m2=260,
                    capacity={"banquet": 120, "theatre": 200},
                ),
            ),
        ),
    ]
    for number in _ROOM_FLOORS:
        rooms = [
            showroom_api.Room(
                number=f"{number}{index:02d}", side=side, type_key=_room_type(number, side)
            )
            for index, side in enumerate(_SIDES_ALONG_FLOOR, start=1)
        ]
        floors.append(
            showroom_api.Floor(number=number, kind="rooms", sellable=True, rooms=tuple(rooms))
        )
    floors.append(
        showroom_api.Floor(
            number=_FLOORS_TOTAL, kind="restaurant", label=_text("Sky bar", "Скай-бар", "Скай-бар")
        )
    )
    return showroom_api.SceneDocument(
        building=showroom_api.Building(
            floors_total=_FLOORS_TOTAL,
            length_m=64,
            width_m=18,
            orientation_deg=90,
            rounded_ends=True,
            facade_step=True,
            podium_floors=2,
            crown=True,
            neighbours=(
                showroom_api.Neighbour(
                    kind="building",
                    names=_text("Congress hall", "Конгресс-холл", "Конгресс-холл"),
                    azimuth_deg=135,
                    distance_m=150,
                    length_m=80,
                    width_m=110,
                    height_m=20,
                ),
                showroom_api.Neighbour(
                    kind="street",
                    names=_text("Central avenue", "Центральный проспект", "Орталық даңғыл"),
                    azimuth_deg=340,
                    distance_m=120,
                ),
            ),
        ),
        sides={
            "north": showroom_api.SideInfo(
                names=_text("City side", "Сторона города", "Қала жағы"), view_keys=("city",)
            ),
            "south": showroom_api.SideInfo(
                names=_text("Mountain side", "Сторона гор", "Тау жағы"), view_keys=("mountains",)
            ),
            "east": showroom_api.SideInfo(
                names=_text("East corner", "Восточный угол", "Шығыс бұрыш"),
                view_keys=("mountains", "city"),
            ),
            "west": showroom_api.SideInfo(
                names=_text("West corner", "Западный угол", "Батыс бұрыш"),
                view_keys=("mountains", "city"),
            ),
        },
        views={
            "mountains": showroom_api.ViewInfo(
                names=_text("Mountains", "Горы", "Таулар"), azimuth_deg=180, distance_m=15000
            ),
            "city": showroom_api.ViewInfo(names=_text("City", "Город", "Қала"), azimuth_deg=0),
        },
        room_types={
            "standard": showroom_api.RoomTypeInfo(
                names=_text("Standard", "Стандарт", "Стандарт"),
                price_note=_text(
                    "from 40,000 ₸ per night", "от 40 000 ₸ за ночь", "түнге 40 000 ₸ бастап"
                ),
            ),
            "deluxe": showroom_api.RoomTypeInfo(
                names=_text("Deluxe corner", "Делюкс угловой", "Делюкс бұрыштық"), windows=2
            ),
            "suite": showroom_api.RoomTypeInfo(
                names=_text("Suite", "Люкс", "Люкс"),
                windows=2,
                note=_text("Two rooms", "Две комнаты", "Екі бөлме"),
            ),
        },
        floors=tuple(floors),
    )


async def seed_demo_showroom(tenant_id: uuid.UUID) -> None:
    """Службы, настройки и сцена витрины демо-отеля (идемпотентно)."""
    with tenant_context(tenant_id):
        for category in DEMO_SHOWROOM_CATEGORIES:
            try:
                await create_category(category)
            except AppError as error:
                if error.code != ERR_REQUESTS_CATEGORY_KEY_TAKEN:
                    raise
    async with platform_session_scope() as session:
        await mutate_tenant_config(session, tenant_id, _with_demo_showroom)
    with tenant_context(tenant_id):
        if await showroom_api.has_scene():
            logger.info("demo_showroom_scene_skipped")
            return
        await showroom_api.replace_scene(build_demo_scene(), acting_user=None)


def _with_demo_showroom(config: TenantConfig) -> TenantConfig:
    if config.showroom is not None:
        return config
    return config.model_copy(update={"showroom": DEMO_SHOWROOM})
