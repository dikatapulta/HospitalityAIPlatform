"""Одноразовая ссылка привязки Stay — QR у стойки (spec 0033 §6, ADR-008 §3).

Ресепшен выпускает ссылку с карточки заселения, гость сканирует QR и после
consent-строки привязывается БЕЗ ввода кода. Хранилище — Redis (канон-
инфраструктура уже есть, spec 0023): ключ `bindlink:{tenant}:{sha256(token)}` →
`stay_id`, TTL 120 с, потребление — атомарный GETDEL (одноразовость держит сам
Redis, гонка двух сканов невозможна). Таблица не нужна: эфемерность — свойство,
потеря при рестарте Redis лечится перевыпуском одним нажатием.

В отличие от rate-limit-канона здесь FAIL-CLOSED: недоступный Redis не выдаёт
(ошибка уходит персоналу — QR, который молча не сработает у гостя, хуже) и не
принимает (потребление отвечает «ссылка истекла») — у гостя всегда остаётся
путь через ввод кода. Ключ начинается с tenant_id (P-4: у Redis нет RLS,
изоляция держится дисциплиной ключей) — ссылка чужого тенанта бесполезна.

Rate-limit'ы (канон 0023) — забота вызывающих, как у ввода кода (spec 0027
§3.3): выпуск — кабинет по (tenant, stay), потребление — канал web по IP.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from contextlib import suppress
from typing import Any, Final, Protocol

import redis.asyncio as redis

from hospitality.modules.guests.service import _get_active_stay_or_raise
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.ratelimit import create_redis_client
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8): Redis недоступен при
# ВЫПУСКЕ ссылки — персоналу отвечаем явно (fail-closed), гость входит по коду.
ERR_GUESTS_BINDLINK_UNAVAILABLE = "ERR-GUESTS-005"

# Срок жизни ссылки (spec 0033 §6): гость сканирует QR у стойки в момент
# показа — двух минут достаточно, истёкшая перевыпускается одним нажатием.
BIND_LINK_TTL_SECONDS: Final = 120


class BindLinkRedis(Protocol):
    """Подмножество `redis.asyncio.Redis`, нужное ссылке (точка подмены в тестах,
    канон `RateLimitRedis`)."""

    def set(self, name: str, value: str, ex: int) -> Any: ...

    def getdel(self, name: str) -> Any: ...


def _key(token: str) -> str:
    # В Redis — только SHA-256 хэш токена (канон секретов ADR-008: plaintext
    # живёт лишь в выданной ссылке); tenant_id — первым сегментом (P-4).
    digest = hashlib.sha256(token.encode()).hexdigest()
    return f"bindlink:{current_tenant_id()}:{digest}"


async def issue_bind_link(stay_id: uuid.UUID, *, client: BindLinkRedis | None = None) -> str:
    """Выпустить одноразовую ссылку привязки активного Stay; вернуть токен.

    Токен показывается РОВНО ОДИН РАЗ (внутри QR/URL); повторный выпуск ничего
    не гасит — старые ссылки доживают свой TTL (повторное открытие страницы
    ничего не ломает, spec 0033 §6). Нет активного Stay — ERR-GUESTS-001;
    недоступный Redis — ошибка вызывающему (fail-closed, см. докстринг модуля).
    """
    async with session_scope() as session:
        stay = await _get_active_stay_or_raise(session, stay_id)
    token = secrets.token_urlsafe(32)
    own_client: redis.Redis | None = None
    if client is None:
        client = own_client = create_redis_client()
    try:
        await client.set(_key(token), str(stay_id), ex=BIND_LINK_TTL_SECONDS)
    except (OSError, redis.RedisError, TimeoutError):
        logger.warning("bind_link_backend_unavailable", exc_info=True)
        raise AppError(
            code=ERR_GUESTS_BINDLINK_UNAVAILABLE,
            message="Bind-link storage is unavailable — the guest can enter the code instead",
            status_code=503,
        ) from None
    finally:
        if own_client is not None:
            with suppress(Exception):
                await own_client.aclose()
    logger.info("stay_bind_link_issued", stay_id=str(stay_id), room_number=stay.room_number)
    return token


async def consume_bind_link(token: str, *, client: BindLinkRedis | None = None) -> uuid.UUID | None:
    """Потребить ссылку (атомарный GETDEL): stay_id или None.

    None — истекла, уже потреблена, чужой тенант или Redis недоступен
    (fail-closed): исходы намеренно неразличимы, гостю канал отвечает одним
    текстом «попросите показать QR ещё раз».
    """
    own_client: redis.Redis | None = None
    if client is None:
        client = own_client = create_redis_client()
    try:
        raw = await client.getdel(_key(token))
    except (OSError, redis.RedisError, TimeoutError):
        logger.warning("bind_link_backend_unavailable", exc_info=True)
        return None
    finally:
        if own_client is not None:
            with suppress(Exception):
                await own_client.aclose()
    if raw is None:
        logger.info("stay_bind_link_rejected", reason="expired_or_consumed")
        return None
    value = raw.decode() if isinstance(raw, bytes) else str(raw)
    logger.info("stay_bind_link_consumed", stay_id=value)
    return uuid.UUID(value)
