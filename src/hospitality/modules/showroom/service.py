"""Сервис модуля showroom: сцена, подбор номеров, код доступа (spec 0069 §2.1–§2.2, §4).

Все функции — внутри `tenant_context` (P-4): сцена читается под RLS, настройки — из
конфига тенанта. Код доступа: в конфиге только bcrypt-хэш (канон кода заселения
spec 0027 §1.2), сам код не хранится и не логируется.

Подпись cookie доступа — HMAC-SHA256 над `tenant_id:access_code_version`; ключ —
производный от bcrypt-хэша кода, а не отдельный секрет деплоймента: у хэша своя
случайная соль, он лежит только в БД, и смена кода меняет ключ вместе с версией —
прежние cookies гаснут дважды. Отдельной переменной окружения, которую основателю
пришлось бы вписать в `.env` staging к показу, нет.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import uuid
from typing import Final

import bcrypt
from sqlalchemy import select

from hospitality.modules.showroom.models import ShowroomScene
from hospitality.modules.showroom.scene import Floor, Room, SceneDocument
from hospitality.modules.showroom.schemas import RoomMatch, RoomQuery, SceneRead
from hospitality.platform.config import (
    TENANT_NOT_CONFIGURED_ERROR_CODE,
    TenantConfig,
    load_tenant_config,
    mutate_tenant_config,
)
from hospitality.platform.showroom_config import ShowroomConfig
from hospitality.platform.staff_actions import (
    StaffAction,
    StaffActionRecord,
    StaffActor,
    record_staff_action,
)
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8); 002 — `scene.py`.
ERR_SHOWROOM_DISABLED: Final = "ERR-SHOWROOM-001"
ERR_SHOWROOM_RATE_LIMITED: Final = "ERR-SHOWROOM-003"
ERR_SHOWROOM_ACCESS_DENIED: Final = "ERR-SHOWROOM-005"

# Код один на отель, не короче 8 знаков (§2.2); потолок — 72 байта bcrypt.
MIN_ACCESS_CODE_LENGTH: Final = 8
MAX_ACCESS_CODE_LENGTH: Final = 64
_BCRYPT_MAX_BYTES: Final = 72
_COOKIE_KEY_LABEL: Final = b"showroom-access-cookie:"


async def load_settings() -> ShowroomConfig | None:
    """Витрина отеля из конфига; конфига нет (онбординг не завершён) — None."""
    try:
        config = await _load_config()
    except AppError as error:
        if error.code == TENANT_NOT_CONFIGURED_ERROR_CODE:
            return None
        raise
    return config.showroom


async def _load_config() -> TenantConfig:
    async with session_scope() as session:
        return await load_tenant_config(session, current_tenant_id())


async def has_scene() -> bool:
    """Загружена ли сцена у отеля — без разбора документа."""
    async with session_scope() as session:
        found = await session.scalar(select(ShowroomScene.id).limit(1))
    return found is not None


async def get_scene() -> SceneRead | None:
    """Сцена отеля; не загружена — None."""
    async with session_scope() as session:
        row = await session.scalar(select(ShowroomScene))
    if row is None:
        return None
    return SceneRead(
        scene=SceneDocument.model_validate(row.scene),
        scene_version=row.scene_version,
        replaced_at=row.replaced_at,
    )


async def replace_scene(document: SceneDocument, *, acting_user: StaffActor | None) -> SceneRead:
    """Заменить сцену отеля целиком: версия +1, строка журнала с личностью (§2.1).

    Строка тенанта одна (`tenant_id` UNIQUE); блокировка `FOR UPDATE` делает две
    загрузки разом последовательными — вторая получает следующую версию. Без
    личности (демо-сид) журнал не пишется (spec 0041 §2.2).
    """
    now = utc_now()
    async with session_scope() as session:
        row = await session.scalar(select(ShowroomScene).with_for_update())
        payload = document.model_dump(mode="json")
        user_id = acting_user.user_id if acting_user else None
        if row is None:
            row = ShowroomScene(
                scene=payload, scene_version=1, replaced_by_user_id=user_id, replaced_at=now
            )
            session.add(row)
        else:
            row.scene = payload
            row.scene_version += 1
            row.replaced_by_user_id = user_id
            row.replaced_at = now
        if acting_user is not None:
            record_staff_action(
                session, acting_user, StaffActionRecord(action=StaffAction.SHOWROOM_SCENE_REPLACED)
            )
        await session.flush()
        version = row.scene_version
    logger.info(
        "showroom_scene_replaced",
        scene_version=version,
        floors=len(document.floors),
        rooms=len(document.rooms()),
        user_id=str(user_id) if user_id else None,
    )
    return SceneRead(scene=document, scene_version=version, replaced_at=now)


async def find_rooms(query: RoomQuery) -> list[RoomMatch]:
    """Номера сцены под условия (§4); сцены нет — пустой список."""
    current = await get_scene()
    if current is None:
        return []
    return match_rooms(current.scene, query)


def match_rooms(scene: SceneDocument, query: RoomQuery) -> list[RoomMatch]:
    """Подбор по сцене: этажи, сторона, вид, тип; порядок — этаж сверху вниз, затем номер.

    Вид номера — его `view_keys`, а если они пусты — виды его стороны.
    """
    matches = [
        _match(floor, room, scene)
        for floor, room in scene.rooms()
        if _fits(floor, room, scene, query)
    ]
    matches.sort(key=lambda match: (-match.floor, match.number.zfill(10)))
    return matches[: query.count] if query.count is not None else matches


def room_views(scene: SceneDocument, room: Room) -> tuple[str, ...]:
    """Виды из номера: свои или его стороны."""
    if room.view_keys:
        return room.view_keys
    side = scene.sides.get(room.side)
    return side.view_keys if side else ()


def _fits(floor: Floor, room: Room, scene: SceneDocument, query: RoomQuery) -> bool:
    if query.floor_from is not None and floor.number < query.floor_from:
        return False
    if query.floor_to is not None and floor.number > query.floor_to:
        return False
    if query.side is not None and room.side != query.side:
        return False
    if query.type_key is not None and room.type_key != query.type_key:
        return False
    return query.view_key is None or query.view_key in room_views(scene, room)


def _match(floor: Floor, room: Room, scene: SceneDocument) -> RoomMatch:
    return RoomMatch(
        number=room.number,
        floor=floor.number,
        side=room.side,
        type_key=room.type_key,
        view_keys=room_views(scene, room),
    )


def check_access_code_format(code: str) -> None:
    """Код годится для `set_access_code`: длина и предел bcrypt. Иначе ValueError."""
    if not MIN_ACCESS_CODE_LENGTH <= len(code) <= MAX_ACCESS_CODE_LENGTH:
        raise ValueError(
            f"код доступа — от {MIN_ACCESS_CODE_LENGTH} до {MAX_ACCESS_CODE_LENGTH} знаков"
        )
    if len(code.encode()) > _BCRYPT_MAX_BYTES:
        raise ValueError(f"код доступа длиннее {_BCRYPT_MAX_BYTES} байт")
    if code != code.strip():
        raise ValueError("код доступа не начинается и не кончается пробелом")


async def set_access_code(code: str) -> int:
    """Новый код доступа витрины (§2.2): хэш в конфиг, версия +1. Возвращает версию.

    Витрина у отеля не настроена — `ValueError`: включает её онбординг, а не команда.
    """
    check_access_code_format(code)
    code_hash = (await asyncio.to_thread(bcrypt.hashpw, code.encode(), bcrypt.gensalt())).decode()

    def with_new_code(config: TenantConfig) -> TenantConfig:
        if config.showroom is None:
            raise ValueError("витрина у отеля не настроена — сначала ключ showroom в профиле")
        showroom = config.showroom.model_copy(
            update={
                "access_code_hash": code_hash,
                "access_code_version": config.showroom.access_code_version + 1,
            }
        )
        return config.model_copy(update={"showroom": showroom})

    async with session_scope() as session:
        updated = await mutate_tenant_config(session, current_tenant_id(), with_new_code)
    assert updated.showroom is not None
    version = updated.showroom.access_code_version
    logger.info("showroom_access_code_set", access_code_version=version)
    return version


async def verify_access_code(code: str) -> bool:
    """Подходит ли код к витрине отеля. Кода нет или он вне пределов — False без bcrypt."""
    settings = await load_settings()
    if settings is None or settings.access_code_hash is None:
        return False
    if not code or len(code) > MAX_ACCESS_CODE_LENGTH or len(code.encode()) > _BCRYPT_MAX_BYTES:
        return False
    return await asyncio.to_thread(
        bcrypt.checkpw, code.encode(), settings.access_code_hash.encode()
    )


def access_token(settings: ShowroomConfig, tenant_id: uuid.UUID) -> str | None:
    """Значение cookie доступа для текущего кода; кода нет — None."""
    if settings.access_code_hash is None:
        return None
    key = hashlib.sha256(_COOKIE_KEY_LABEL + settings.access_code_hash.encode()).digest()
    message = f"{tenant_id}:{settings.access_code_version}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def token_matches(settings: ShowroomConfig, tenant_id: uuid.UUID, presented: str | None) -> bool:
    """Cookie посетителя подписана текущим кодом отеля (сравнение за постоянное время)."""
    expected = access_token(settings, tenant_id)
    if expected is None or not presented:
        return False
    return hmac.compare_digest(expected, presented)
