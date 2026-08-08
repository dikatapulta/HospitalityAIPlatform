"""Креденшелы персонала: нормализация email, пароли, rate-limit входа (spec 0033 §3).

Выделено из `staff_auth.py` (R-3, рекомендация ревью PR #153): здесь — всё,
что доказывает пароль и держит перебор, БЕЗ сессий и ролей. Потребители —
`staff_auth.login` (вход), `staff_invites.accept_invite` (принятие инвайта
существующим email — та же дверь, где доказывается пароль) и CLI бутстрапа
(`tools/staff_bootstrap`).

Бюджет попыток входа тратят только НЕУДАЧИ (issue #207): `enforce_login_rate_limit`
читает счётчик до проверки пароля, `record_failed_login` списывает после отказа.

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
from hospitality.shared.ratelimit import consume_rate_limit, peek_rate_limit

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


def ensure_password_policy(password: str) -> None:
    """Единственный источник ERR-AUTH-007 (P-12): проверка минимальной длины.

    Зовётся ДО поиска личности везде, где пароль задаётся формой. Иначе форма
    отвечает по-разному на занятый и свободный email (блокер ревью PR #159:
    длина проверялась только внутри `hash_password`, то есть только в ветке
    нового User, — короткий пароль давал 422 «свободен» против 401 «занят», и
    страница приглашения становилась оракулом перечисления учёток отеля).
    """
    if len(password) < PASSWORD_MIN_LENGTH:
        raise AppError(
            code=ERR_AUTH_PASSWORD_TOO_SHORT,
            message=f"Password must be at least {PASSWORD_MIN_LENGTH} characters long",
            status_code=422,
        )


async def hash_password(password: str) -> str:
    """argon2id-хэш пароля; проверяет минимальную длину (ERR-AUTH-007)."""
    ensure_password_policy(password)
    return await asyncio.to_thread(_password_hasher.hash, password)


async def verify_password(password: str, secret_hash: str) -> bool:
    """Проверка пароля против argon2-хэша; любой невалидный исход — False."""
    try:
        return await asyncio.to_thread(_password_hasher.verify, secret_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def _login_rate_limit_keys(email: str, client_ip: str) -> tuple[tuple[str, str, int], ...]:
    """Два ключа бюджета входа и их лимиты (§3.3): (scope, key, limit).

    Ключи разные, потому что разный субъект: за email стоит один человек
    (подбор пароля к учётке), за IP — все, кто вышел в интернет через этот
    адрес (перебор учёток). Третьего ключа «email+IP» нет намеренно: он
    строго слабее email-ключа (тот и так считает попытки со всех адресов
    сразу) и подбор с ротацией адресов не ловит вовсе.

    Email в ключ Redis уходит хэшем — PII вне Redis (правило 2 PII_REGISTRY).
    """
    settings = get_settings()
    return (
        (
            "staff_login_email",
            hashlib.sha256(email.encode()).hexdigest(),
            settings.staff_login_rate_limit_attempts,
        ),
        ("staff_login_ip", client_ip, settings.staff_login_ip_rate_limit_attempts),
    )


async def enforce_login_rate_limit(email: str, client_ip: str) -> None:
    """Не исчерпан ли бюджет попыток — ДО проверки пароля (канон 0023, §3.3).

    Бюджет здесь только читается: тратит его одна `record_failed_login`, и
    только неудачная попытка (issue #207). Иначе успешные входы съедали
    общий IP-бюджет отеля, и день раздачи доступов умирал на одиннадцатом
    сотруднике, а утренний заступ смены получал «слишком много попыток» без
    единой ошибки пароля.

    Общий бюджет для ВСЕХ дверей, где доказывается пароль: login и принятие
    инвайта существующим email (`staff_invites.accept_invite`, блокер ревью
    PR #148) — троттлинг, обходимый соседней дверью, не троттлинг."""
    window_seconds = get_settings().staff_login_rate_limit_window_seconds
    for scope, key, limit in _login_rate_limit_keys(email, client_ip):
        if limit <= 0:
            continue
        decision = await peek_rate_limit(scope, key, limit=limit, window_seconds=window_seconds)
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


async def record_failed_login(email: str, client_ip: str) -> None:
    """Списать неудачную попытку с обоих бюджетов (issue #207).

    Зовётся ровно там, где отказ означает недоказанный пароль (ERR-AUTH-001):
    неизвестный email, неверный пароль. Отказ доказавшему пароль — например,
    деактивированному сотруднику (ERR-AUTH-005) — попыткой подбора не
    является и бюджет не тратит: иначе уволенный сотрудник, чей телефон сам
    повторяет вход, выжигал бы лимит живой смене за тем же NAT.
    """
    window_seconds = get_settings().staff_login_rate_limit_window_seconds
    for scope, key, limit in _login_rate_limit_keys(email, client_ip):
        if limit <= 0:
            continue
        await consume_rate_limit(scope, key, limit=limit, window_seconds=window_seconds)
