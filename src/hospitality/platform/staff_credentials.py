"""Креденшелы персонала: логин отеля, пароли, rate-limit входа (spec 0033 §3, spec 0037).

Выделено из `staff_auth.py` (R-3, рекомендация ревью PR #153): здесь — всё,
что доказывает пароль и держит перебор, БЕЗ сессий и ролей. Потребители —
`staff_auth.login` (вход, единственная дверь, где доказывается пароль),
`staff_invites` (логин из приглашения и пароль новой учётки) и CLI бутстрапа
(`tools/staff_bootstrap`).

Логин отеля (spec 0037 §2): 3–12 латинских букв и цифр, первой — буква,
хранится заглавными. Уникален внутри отеля, поэтому в `user_identities` он
живёт строкой `<tenant_id>:<LOGIN>` (ADR-008 §1, ревизия 27.09.2026) — собирает
и разбирает её только пара `password_external_id`/`parse_password_external_id`.

Бюджет попыток входа тратят только НЕУДАЧИ (issue #207): `enforce_login_rate_limit`
читает счётчик до проверки пароля, `record_failed_login` списывает после отказа.

Криптографика (канон — guests/service.py, обоснование spec 0033 §3.1/§3.3):
- пароль: argon2id (`argon2-cffi`), в БД только хэш; argon2 блокирует поток
  (~десятки мс) — hash/verify уходят в `asyncio.to_thread`;
- выравнивание времени отказа: `TIMING_EQUALIZER_HASH` — verify фиктивного
  хэша, чтобы «нет такого логина» не отвечал быстрее «пароль не подошёл».

Логин — PII (производное от имени, PII_REGISTRY): в логи не пишется никогда
(правило 2), в ключ rate-limit уходит SHA-256 пары «отель + логин».
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
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
ERR_AUTH_LOGIN_INVALID = "ERR-AUTH-012"

# Формат логина отеля (spec 0037 §2) — после нормализации (trim + upper).
# Кириллицы нет намеренно: `В`, `О`, `М` неотличимы от латинских на экране.
LOGIN_PATTERN: Final = re.compile(r"[A-Z][A-Z0-9]{2,11}")
LOGIN_MAX_LENGTH: Final = 12

# Минимальная длина пароля: единственное правило v1 (P-1: без zxcvbn-эвристик;
# фактическая стойкость входа держится argon2 + rate-limit по логину и IP).
PASSWORD_MIN_LENGTH: Final = 8

# Параметры argon2id по умолчанию argon2-cffi (RFC 9106 low-memory профиль)
# устраивают v1; смена параметров обратносовместима — verify читает их из хэша.
_password_hasher: Final = PasswordHasher()

# argon2-хэш заведомо несуществующего пароля — выравнивание времени ответа
# (канон _TIMING_EQUALIZER_HASH guests/service.py): отказ «нет такого логина»
# не должен отвечать быстрее отказа «пароль не подошёл».
TIMING_EQUALIZER_HASH: Final = (
    "$argon2id$v=19$m=65536,t=3,p=4$uFVDnGZFgOmMSDz42FpQyQ"
    "$YMNScKQ9hG18S798MuuewCjeBlO2LCiM4kr/AqvxWc8"
)


def normalize_login(raw: str) -> str | None:
    """Канон логина отеля (spec 0037 §2): trim + upper; не тот формат — None.

    ASCII проверяется ДО `upper()`: иначе `ß` стал бы `SS`, а турецкая `ı` —
    `I`, и у логина появились бы невидимые синонимы.
    """
    stripped = raw.strip()
    if not stripped.isascii():
        return None
    login = stripped.upper()
    return login if LOGIN_PATTERN.fullmatch(login) else None


def require_login_format(raw: str) -> str:
    """Нормализованный логин или ERR-AUTH-012 (422): формат — не секрет,
    отказ по нему не тратит бюджет попыток и ничего не говорит об учётках."""
    login = normalize_login(raw)
    if login is None:
        raise AppError(
            code=ERR_AUTH_LOGIN_INVALID,
            message="Login must be 3-12 Latin letters or digits starting with a letter",
            status_code=422,
        )
    return login


def password_external_id(tenant_id: uuid.UUID, login: str) -> str:
    """`external_id` идентичности `kind=password`: `<tenant_id>:<LOGIN>`.

    Уникальность логина внутри отеля даёт существующий UNIQUE `(kind,
    external_id)` — платформенной таблице не нужна колонка `tenant_id`
    (нового исключения из P-4 нет, spec 0037 §6). Формат строки живёт только
    здесь и в `parse_password_external_id`."""
    return f"{tenant_id}:{login}"


def parse_password_external_id(external_id: str) -> tuple[uuid.UUID, str] | None:
    """Обратная операция: (tenant_id, логин) или None для строки другого
    формата (старый email, мусор) — такая учётка показывается «без логина»."""
    tenant_part, separator, login = external_id.partition(":")
    if not separator or normalize_login(login) != login:
        return None
    try:
        return uuid.UUID(tenant_part), login
    except ValueError:
        return None


def ensure_password_policy(password: str) -> None:
    """Единственный источник ERR-AUTH-007 (P-12): проверка минимальной длины.

    Зовётся ДО любой работы с БД везде, где пароль задаётся формой: короткий
    пароль отклоняется одинаково, что бы ни лежало в базе (урок блокера ревью
    PR #159 — тогда длина разводила исходы и делала страницу приглашения
    оракулом перечисления учёток).
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


def _login_rate_limit_keys(
    tenant_id: uuid.UUID, login: str, client_ip: str
) -> tuple[tuple[str, str, int], ...]:
    """Два ключа бюджета входа и их лимиты (§3.3): (scope, key, limit).

    Ключи разные, потому что разный субъект: за парой «отель + логин» стоит
    один человек (подбор пароля к учётке), за IP — все, кто вышел в интернет
    через этот адрес (перебор учёток). Третьего ключа «учётка+IP» нет
    намеренно: он строго слабее ключа учётки (тот и так считает попытки со
    всех адресов сразу) и подбор с ротацией адресов не ловит вовсе.

    Пара «отель + логин», а не один логин (spec 0037 §3): `BORM` бывает в
    двух отелях, и подбор к одному не должен запирать другого. Логин в ключ
    Redis уходит хэшем — PII вне Redis (правило 2 PII_REGISTRY).
    """
    settings = get_settings()
    return (
        (
            "staff_login_account",
            hashlib.sha256(password_external_id(tenant_id, login).encode()).hexdigest(),
            settings.staff_login_rate_limit_attempts,
        ),
        ("staff_login_ip", client_ip, settings.staff_login_ip_rate_limit_attempts),
    )


async def enforce_login_rate_limit(tenant_id: uuid.UUID, login: str, client_ip: str) -> None:
    """Не исчерпан ли бюджет попыток — ДО проверки пароля (канон 0023, §3.3).

    Бюджет здесь только читается: тратит его одна `record_failed_login`, и
    только неудачная попытка (issue #207). Иначе успешные входы съедали
    общий IP-бюджет отеля, и день раздачи доступов умирал на одиннадцатом
    сотруднике, а утренний заступ смены получал «слишком много попыток» без
    единой ошибки пароля.

    Дверь, где доказывается пароль, одна — `staff_auth.login`: принятие
    приглашения всегда заводит новую учётку и чужой пароль не проверяет
    (spec 0037 §4), поэтому обходной двери в обход лимита нет."""
    window_seconds = get_settings().staff_login_rate_limit_window_seconds
    for scope, key, limit in _login_rate_limit_keys(tenant_id, login, client_ip):
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


async def record_failed_login(tenant_id: uuid.UUID, login: str, client_ip: str) -> None:
    """Списать неудачную попытку с обоих бюджетов (issue #207).

    Зовётся ровно там, где отказ означает недоказанный пароль (ERR-AUTH-001):
    неизвестный логин, неверный пароль. Отказ доказавшему пароль — например,
    деактивированному сотруднику (ERR-AUTH-005) — попыткой подбора не
    является и бюджет не тратит: иначе уволенный сотрудник, чей телефон сам
    повторяет вход, выжигал бы лимит живой смене за тем же NAT.
    """
    window_seconds = get_settings().staff_login_rate_limit_window_seconds
    for scope, key, limit in _login_rate_limit_keys(tenant_id, login, client_ip):
        if limit <= 0:
            continue
        await consume_rate_limit(scope, key, limit=limit, window_seconds=window_seconds)
