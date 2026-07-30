"""Креденшелы персонала: нормализация email, пароли, rate-limit входа (spec 0033 §3).

Выделено из `staff_auth.py` (R-3, рекомендация ревью PR #153): здесь — всё,
что доказывает пароль и держит перебор, БЕЗ сессий и ролей. Потребители —
`staff_auth.login` (вход), `staff_invites.accept_invite` (принятие инвайта
существующим email — та же дверь, где доказывается пароль) и CLI бутстрапа
(`tools/staff_bootstrap`).

Криптографика (канон — guests/service.py, обоснование spec 0033 §3.1/§3.3):
- пароль: argon2id (`argon2-cffi`), в БД только хэш; argon2 блокирует поток
  (~десятки мс) — hash/verify уходят в `asyncio.to_thread`;
- выравнивание времени отказа: `TIMING_EQUALIZER_HASH` — verify фиктивного
  хэша, чтобы «нет такого email» не отвечал быстрее «пароль не подошёл».

Email — PII: в логи не пишется никогда (правило 2 PII_REGISTRY), в ключ
rate-limit уходит его SHA-256.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from hospitality.shared.config import get_settings
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_staff_login
from hospitality.shared.ratelimit import consume_rate_limit

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_AUTH_LOGIN_RATE_LIMITED = "ERR-AUTH-006"
ERR_AUTH_PASSWORD_TOO_SHORT = "ERR-AUTH-007"

# Минимальная длина пароля: единственное правило v1 (P-1: без zxcvbn-эвристик;
# фактическая стойкость входа держится argon2 + rate-limit по email и IP).
PASSWORD_MIN_LENGTH: Final = 8

# Параметры argon2id по умолчанию argon2-cffi (RFC 9106 low-memory профиль)
# устраивают v1; смена параметров обратносовместима — verify читает их из хэша.
_password_hasher: Final = PasswordHasher()

# argon2-хэш заведомо несуществующего пароля — выравнивание времени ответа
# (канон _TIMING_EQUALIZER_HASH guests/service.py): отказ «нет такого email»
# не должен отвечать быстрее отказа «пароль не подошёл».
TIMING_EQUALIZER_HASH: Final = (
    "$argon2id$v=19$m=65536,t=3,p=4$uFVDnGZFgOmMSDz42FpQyQ"
    "$YMNScKQ9hG18S798MuuewCjeBlO2LCiM4kr/AqvxWc8"
)


def normalize_email(raw: str) -> str:
    """Канон нормализации email-логина (spec 0033 §3.1): trim + lowercase."""
    return raw.strip().lower()


async def hash_password(password: str) -> str:
    """argon2id-хэш пароля; проверяет минимальную длину (ERR-AUTH-007)."""
    if len(password) < PASSWORD_MIN_LENGTH:
        raise AppError(
            code=ERR_AUTH_PASSWORD_TOO_SHORT,
            message=f"Password must be at least {PASSWORD_MIN_LENGTH} characters long",
            status_code=422,
        )
    return await asyncio.to_thread(_password_hasher.hash, password)


async def verify_password(password: str, secret_hash: str) -> bool:
    """Проверка пароля против argon2-хэша; любой невалидный исход — False."""
    try:
        return await asyncio.to_thread(_password_hasher.verify, secret_hash, password)
    except (VerificationError, InvalidHashError):
        return False


async def enforce_login_rate_limit(email: str, client_ip: str) -> None:
    """Канон 0023, двойной ключ (§3.3): email (подбор пароля к учётке) и IP
    (перебор учёток). Email в ключ Redis уходит хэшем — PII вне Redis.

    Общий бюджет для ВСЕХ дверей, где доказывается пароль: login и принятие
    инвайта существующим email (`staff_invites.accept_invite`, блокер ревью
    PR #148) — троттлинг, обходимый соседней дверью, не троттлинг."""
    settings = get_settings()
    limit = settings.staff_login_rate_limit_attempts
    if limit <= 0:
        return
    keys = (
        ("staff_login_email", hashlib.sha256(email.encode()).hexdigest()),
        ("staff_login_ip", client_ip),
    )
    for scope, key in keys:
        decision = await consume_rate_limit(
            scope,
            key,
            limit=limit,
            window_seconds=settings.staff_login_rate_limit_window_seconds,
        )
        # Fail-open при недоступном Redis — канон 0023 (стойкость держит argon2).
        if decision.available and not decision.allowed:
            logger.warning(
                "staff.login_rate_limited",
                scope=scope,
                count=decision.count,
                limit=decision.limit,
            )
            record_staff_login("rate_limited")
            raise AppError(
                code=ERR_AUTH_LOGIN_RATE_LIMITED,
                message="Too many login attempts — try again later",
                status_code=429,
            )
