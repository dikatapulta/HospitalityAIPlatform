"""Бизнес-логика модуля guests (spec 0027, ADR-008 §3–§4).

Каждая функция — одна транзакция по канону P-4/P-12: вызывается внутри
`tenant_context`, открывает `session_scope()`; события публикуются в той же
транзакции, что бизнес-запись (P-6). Ожидаемые ошибки персонала — `AppError`
с кодами каталога (R-8); отказ привязки и невалидная сессия — НЕ ошибки,
а `None`: для гостя это штатный путь строгого auth-only (Q7/Q8).

Криптографика (обоснование — spec 0027 §1.2–1.3):
- код заселения: 6 символов алфавита без спутываемых (30⁶ ≈ 7.3×10⁸),
  в БД — bcrypt; проверка не ищет по хэшу — тройка тенант+комната+код ведёт
  к единственному активному коду Stay и одному bcrypt-verify;
- токен сессии: `secrets.token_urlsafe(32)` (256 бит), в БД — SHA-256.
bcrypt блокирует поток (~сотни мс) — hashpw/checkpw уходят в `asyncio.to_thread`,
чтобы не останавливать event loop на время проверки.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import uuid
from typing import Final

import bcrypt
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.guests.events import StayCheckedIn, StayCheckedOut
from hospitality.modules.guests.models import (
    Guest,
    GuestIdentity,
    GuestIdentityKind,
    GuestSession,
    Stay,
    StayAccessCode,
    StayStatus,
)
from hospitality.modules.guests.schemas import (
    ActiveGuestSession,
    GuestSessionGrant,
    GuestSessionStart,
    StayCheckIn,
    StayCheckInResult,
    StayRead,
)
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.events import publish
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_GUESTS_STAY_NOT_FOUND = "ERR-GUESTS-001"
ERR_GUESTS_ROOM_OCCUPIED = "ERR-GUESTS-002"
ERR_GUESTS_CODE_REISSUE_CONFLICT = "ERR-GUESTS-003"

# Алфавит кода заселения: без визуально спутываемых 0/O/1/I/L/U (spec 0027 §1.2).
ACCESS_CODE_ALPHABET: Final = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
ACCESS_CODE_LENGTH: Final = 6

# Имя partial unique индекса активного Stay комнаты (models.Stay) — опознаётся
# в тексте IntegrityError: гонка двух одновременных заселений одной комнаты.
_ACTIVE_STAY_CONSTRAINT: Final = "uq_stays_tenant_room_checked_in"

# last_used_at обновляется не чаще раза в этот интервал (не писать на каждый
# poll веб-чата; наблюдаемость мёртвых сессий не требует точности до секунды).
_LAST_USED_REFRESH_SECONDS: Final = 300

# bcrypt-хэш заведомо несуществующего кода — для выравнивания времени ответа
# (ревью PR #112): без него отказ «нет активного Stay / нет кода» отвечал бы
# заметно быстрее отказа «код не подошёл», выдавая занятость комнаты по времени
# ответа. Значение — хэш строки вне алфавита кодов, verify всегда False.
_TIMING_EQUALIZER_HASH: Final = "$2b$12$/KYwo08e1wAjlr4JXJaugeW4cLdBLjeZBqno/Hj/dYsDBe9In/2UG"


def normalize_access_code(raw: str) -> str:
    """Терпимая нормализация ввода гостя: регистр, пробелы, дефисы.

    `k7m 9qt` == `K7M-9QT` == `k7m9qt` (spec 0027 §1.2). Валидацию по алфавиту
    не делаем: неверный код и так не пройдёт bcrypt-verify, а «почти угадал»
    гостю не сообщается.
    """
    return raw.upper().replace("-", "").replace(" ", "")


def format_access_code(code: str) -> str:
    """Код для показа человеку: `K7M9QT` → `K7M-9QT` (читабельность)."""
    return f"{code[:3]}-{code[3:]}"


def _generate_access_code() -> str:
    return "".join(secrets.choice(ACCESS_CODE_ALPHABET) for _ in range(ACCESS_CODE_LENGTH))


def _hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _bcrypt_hash(code: str) -> str:
    return (await asyncio.to_thread(bcrypt.hashpw, code.encode(), bcrypt.gensalt())).decode()


async def _bcrypt_verify(code: str, code_hash: str) -> bool:
    return await asyncio.to_thread(bcrypt.checkpw, code.encode(), code_hash.encode())


async def check_in(data: StayCheckIn) -> StayCheckInResult:
    """Заселить гостя: Guest + Stay(`checked_in`) + код заселения одной транзакцией.

    Код возвращается в открытом виде РОВНО ОДИН РАЗ (в БД — bcrypt-хэш);
    выдача неотделима от заселения — «шаг нельзя забыть» (ADR-008 §3,
    уточнение spec 0027 §1.2). Комната с активным Stay — ERR-GUESTS-002:
    второе параллельное заселение отвергает partial unique индекс, а не
    гонка проверок.
    """
    code = _generate_access_code()
    code_hash = await _bcrypt_hash(code)
    try:
        async with session_scope() as session:
            guest = Guest(display_name=data.guest_display_name)
            session.add(guest)
            await session.flush()
            stay = Stay(
                guest_id=guest.id,
                room_number=data.room_number,
                status=StayStatus.CHECKED_IN,
                check_in_at=utc_now(),
                check_out_at=data.check_out_at,
            )
            session.add(stay)
            await session.flush()
            session.add(StayAccessCode(stay_id=stay.id, code_hash=code_hash))
            await publish(session, StayCheckedIn(stay_id=stay.id, room_number=stay.room_number))
    except IntegrityError as error:
        if _ACTIVE_STAY_CONSTRAINT not in str(error):
            raise
        raise AppError(
            code=ERR_GUESTS_ROOM_OCCUPIED,
            message=f"Room {data.room_number!r} already has an active stay",
            status_code=409,
        ) from None
    logger.info("stay_checked_in", stay_id=str(stay.id), room_number=stay.room_number)
    return StayCheckInResult(stay=StayRead.model_validate(stay), access_code=code)


async def reissue_access_code(stay_id: uuid.UUID) -> str:
    """Перевыпустить код заселения: новый гасит старый, сессии живут (ADR-008 §3)."""
    code = _generate_access_code()
    code_hash = await _bcrypt_hash(code)
    try:
        async with session_scope() as session:
            stay = await _get_active_stay_or_raise(session, stay_id)
            now = utc_now()
            for active_code in await session.scalars(
                select(StayAccessCode).where(
                    StayAccessCode.stay_id == stay.id, StayAccessCode.revoked_at.is_(None)
                )
            ):
                active_code.revoked_at = now
            await session.flush()
            session.add(StayAccessCode(stay_id=stay.id, code_hash=code_hash))
    except IntegrityError as error:
        # Гонка двух одновременных перевыпусков (ревью PR #112): проигравший
        # налетает на partial unique активного кода. Повтор операции штатен —
        # выигравший код уже показан тому, кто успел первым.
        if "uq_stay_access_codes_active_stay" not in str(error):
            raise
        raise AppError(
            code=ERR_GUESTS_CODE_REISSUE_CONFLICT,
            message="Access code was reissued concurrently — retry to get a fresh code",
            status_code=409,
        ) from None
    logger.info("stay_code_reissued", stay_id=str(stay.id), room_number=stay.room_number)
    return code


async def check_out(stay_id: uuid.UUID) -> StayRead:
    """Выезд: Stay → `checked_out`, код и все сессии гаснут (Q8, ADR-008 §4).

    Ранний выезд правит сам Stay — доступ следует автоматически; после выезда
    канал деградирует до неавторизованного, grace-периода нет.
    """
    async with session_scope() as session:
        stay = await _get_active_stay_or_raise(session, stay_id)
        now = utc_now()
        stay.status = StayStatus.CHECKED_OUT
        stay.check_out_at = min(stay.check_out_at, now)
        for active_code in await session.scalars(
            select(StayAccessCode).where(
                StayAccessCode.stay_id == stay.id, StayAccessCode.revoked_at.is_(None)
            )
        ):
            active_code.revoked_at = now
        for active_session in await session.scalars(
            select(GuestSession).where(
                GuestSession.stay_id == stay.id, GuestSession.revoked_at.is_(None)
            )
        ):
            active_session.revoked_at = now
        await publish(session, StayCheckedOut(stay_id=stay.id, room_number=stay.room_number))
    logger.info("stay_checked_out", stay_id=str(stay.id), room_number=stay.room_number)
    return StayRead.model_validate(stay)


async def find_active_stay(room_number: str) -> StayRead | None:
    """Stay комнаты в `checked_in` — ВЗГЛЯД ПЕРСОНАЛА (CLI/кабинет); None — нет.

    Намеренно БЕЗ фильтра по `check_out_at` (ревью PR #112): просроченный, но
    не выписанный Stay всё ещё занимает комнату (partial unique) — персонал
    обязан его видеть, чтобы оформить выезд или перевыпустить код. Гостевая
    привязка, наоборот, срок проверяет (`_find_bindable_stay`).
    """
    async with session_scope() as session:
        stay: Stay | None = await session.scalar(
            select(Stay).where(
                Stay.room_number == room_number, Stay.status == StayStatus.CHECKED_IN
            )
        )
        return None if stay is None else StayRead.model_validate(stay)


async def list_active_stays() -> list[StayRead]:
    """Все Stay тенанта в `checked_in` (CLI `--list`), по номеру комнаты.

    Просроченные не скрываются (см. `find_active_stay`): они занимают комнаты,
    CLI помечает их и подсказывает выезд.
    """
    async with session_scope() as session:
        stays = await session.scalars(
            select(Stay).where(Stay.status == StayStatus.CHECKED_IN).order_by(Stay.room_number)
        )
        return [StayRead.model_validate(stay) for stay in stays]


async def start_guest_session(data: GuestSessionStart) -> GuestSessionGrant | None:
    """Привязка канала к Stay тройкой тенант+комната+код (ADR-008 §3).

    Тенант — из контекста (P-4: чужой Stay недостижим на уровне СУБД). Любой
    невалидный исход (нет активного Stay, код не подошёл/погашен) — `None`
    без уточнения причины: гостю не сообщается, «почти угадал» ли он, а
    перечисление занятых комнат по разнице ответов невозможно. Rate-limit
    ввода кода — забота канала (spec 0027 §3.3), не домена.

    Код многоразовый в пределах Stay: каждая успешная привязка (второе
    устройство, повторный ввод) — своя `GuestIdentity` и своя сессия.
    """
    code = normalize_access_code(data.code)
    token = secrets.token_urlsafe(32)
    async with session_scope() as session:
        stay = await _find_bindable_stay(session, data.room_number)
        if stay is None:
            # Выравнивание времени ответа: отказ без Stay не должен быть быстрее
            # отказа по неверному коду (см. _TIMING_EQUALIZER_HASH).
            await _bcrypt_verify(code, _TIMING_EQUALIZER_HASH)
            logger.warning("guest_code_rejected", room_number=data.room_number, reason="no_stay")
            return None
        active_code = await session.scalar(
            select(StayAccessCode).where(
                StayAccessCode.stay_id == stay.id, StayAccessCode.revoked_at.is_(None)
            )
        )
        code_hash = active_code.code_hash if active_code is not None else _TIMING_EQUALIZER_HASH
        if not await _bcrypt_verify(code, code_hash) or active_code is None:
            logger.warning(
                "guest_code_rejected", room_number=data.room_number, reason="code_mismatch"
            )
            return None
        identity = await _ensure_identity(session, stay, data)
        guest_session = GuestSession(
            stay_id=stay.id,
            guest_identity_id=identity.id,
            token_hash=_hash_session_token(token),
            consent_version=data.consent_version,
        )
        session.add(guest_session)
        await session.flush()
    logger.info(
        "guest_session_started",
        stay_id=str(stay.id),
        guest_identity_id=str(identity.id),
        session_id=str(guest_session.id),
    )
    return GuestSessionGrant(
        session_token=token,
        stay_id=stay.id,
        guest_identity_id=identity.id,
        room_number=stay.room_number,
    )


async def resolve_session(token: str) -> ActiveGuestSession | None:
    """Валидность сессии НА КАЖДОМ действии (ADR-008 §3): None — auth-only ответ.

    Проверяется всё разом: хэш найден, сессия не отозвана, её Stay в
    `checked_in` и срок не вышел. «Истёкшая сессия не может действовать»
    (DoD #79) выполняется конструктивно — иного пути к данным гостя нет.
    """
    async with session_scope() as session:
        row = (
            await session.execute(
                select(GuestSession, Stay)
                .join(Stay, GuestSession.stay_id == Stay.id)
                .where(GuestSession.token_hash == _hash_session_token(token))
            )
        ).one_or_none()
        if row is None:
            return None
        guest_session, stay = row
        now = utc_now()
        if (
            guest_session.revoked_at is not None
            or stay.status is not StayStatus.CHECKED_IN
            or now >= stay.check_out_at
        ):
            return None
        if (now - guest_session.last_used_at).total_seconds() > _LAST_USED_REFRESH_SECONDS:
            guest_session.last_used_at = now
        return ActiveGuestSession(
            session_id=guest_session.id,
            stay_id=stay.id,
            guest_identity_id=guest_session.guest_identity_id,
            room_number=stay.room_number,
            check_out_at=stay.check_out_at,
        )


async def _ensure_identity(
    session: AsyncSession, stay: Stay, data: GuestSessionStart
) -> GuestIdentity:
    """Идентичность канала для привязки: существующая или новая (гость — из Stay).

    Повторная привязка того же `external_id` (device уже привязывался) находит
    существующую строку; `guest_id` при этом не перевешивается — слияние и
    перенос идентичностей между гостями — отдельная задача (ADR-008 §3,
    «модель поддерживает, автоматика — не сейчас»).
    """
    existing = await session.scalar(
        select(GuestIdentity).where(
            GuestIdentity.kind == data.identity_kind,
            GuestIdentity.external_id == data.identity_external_id,
        )
    )
    if existing is not None:
        return existing
    identity = GuestIdentity(
        guest_id=stay.guest_id,
        kind=data.identity_kind,
        external_id=data.identity_external_id,
    )
    session.add(identity)
    await session.flush()
    return identity


async def _find_bindable_stay(session: AsyncSession, room_number: str) -> Stay | None:
    """Stay, к которому МОЖНО привязаться (гостевой путь): срок не вышел."""
    stay: Stay | None = await session.scalar(
        select(Stay).where(
            Stay.room_number == room_number,
            Stay.status == StayStatus.CHECKED_IN,
            Stay.check_out_at > utc_now(),
        )
    )
    return stay


async def _get_active_stay_or_raise(session: AsyncSession, stay_id: uuid.UUID) -> Stay:
    stay = await session.scalar(
        select(Stay).where(Stay.id == stay_id, Stay.status == StayStatus.CHECKED_IN)
    )
    if stay is None:
        raise AppError(
            code=ERR_GUESTS_STAY_NOT_FOUND,
            message=f"Active stay {stay_id} does not exist",
            status_code=404,
        )
    return stay


# Публичный тип для канала (spec 0027): web создаёт идентичности этого kind.
WEB_IDENTITY_KIND: Final = GuestIdentityKind.WEB
