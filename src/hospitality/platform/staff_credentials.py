"""Креденшелы персонала: логин отеля, пароли, rate-limit входа (spec 0033 §3, spec 0037).

Выделено из `staff_auth.py` (R-3, рекомендация ревью PR #153): здесь — всё,
что доказывает пароль и держит перебор, БЕЗ сессий и ролей. Потребители —
`staff_auth.login` (вход) и `staff_passkeys.require_password` (повторный ввод
под живой сессией, ADR-008 §8) — две двери, где доказывается пароль, с одними
бюджетами; `staff_invites` (логин из приглашения и пароль новой учётки) и CLI
бутстрапа (`tools/staff_bootstrap`).

Логин отеля (spec 0037 §2): 3–12 латинских букв и цифр, первой — буква,
хранится заглавными. Уникален внутри отеля, поэтому в `user_identities` он
живёт строкой `<tenant_id>:<LOGIN>` (ADR-008 §1, ревизия 27.09.2026) — собирает
и разбирает её только пара `password_external_id`/`parse_password_external_id`.

Бюджет попыток входа тратят только НЕУДАЧИ (issue #207): `enforce_login_rate_limit`
читает счётчик до проверки пароля, `record_failed_login` списывает после отказа;
выдуманный логин не тратит бюджет настоящих учёток с того же адреса (issue #486);
знакомое устройство (`staff_devices`, issue #402) идёт мимо ключей учётки и IP — со своим.

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
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from hospitality.platform.staff_password_policy import ensure_password_policy
from hospitality.shared.config import get_settings
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_staff_login
from hospitality.shared.ratelimit import consume_rate_limit, peek_rate_limit

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_AUTH_LOGIN_RATE_LIMITED = "ERR-AUTH-006"
ERR_AUTH_LOGIN_INVALID = "ERR-AUTH-012"

# Формат логина отеля (spec 0037 §2) — после нормализации (trim + upper).
# Кириллицы нет намеренно: `В`, `О`, `М` неотличимы от латинских на экране.
LOGIN_PATTERN: Final = re.compile(r"[A-Z][A-Z0-9]{2,11}")

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


async def hash_password(password: str, *, personal: Iterable[str], hotel: Iterable[str]) -> str:
    """argon2id-хэш пароля; сначала политика (`staff_password_policy`: ERR-AUTH-007,
    ERR-AUTH-016) по логину и имени (`personal`) и словам отеля (`hotel`)."""
    ensure_password_policy(password, personal=personal, hotel=hotel)
    return await asyncio.to_thread(_password_hasher.hash, password)


async def verify_password(password: str, secret_hash: str) -> bool:
    """Проверка пароля против argon2-хэша; любой невалидный исход — False."""
    try:
        return await asyncio.to_thread(_password_hasher.verify, secret_hash, password)
    except (VerificationError, InvalidHashError):
        return False


@dataclass(frozen=True)
class _LoginBudget:
    """Один ключ бюджета входа (§3.3); `spent_by_unknown_login` — списывает ли
    его отказ по логину, которого в отеле нет."""

    scope: str
    key: str
    limit: int
    spent_by_unknown_login: bool


def _login_budgets(
    tenant_id: uuid.UUID, login: str, client_ip: str, device_key: str | None
) -> tuple[_LoginBudget, ...]:
    """Три ключа бюджета входа (spec 0033 §3.3, issue #486) — или один у знакомого устройства.

    - `staff_login_account` — пара «отель + логин»: подбор пароля к одной
      учётке, за ключом один человек. Пара, а не логин (spec 0037 §3): `BORM`
      бывает в двух отелях. Логин в Redis — хэшем (правило 2 PII_REGISTRY).
    - `staff_login_ip` — подбор паролей к настоящим учёткам с одного адреса.
    - `staff_login_ip_flood` — все отказы с адреса, выдуманные логины тоже:
      держит поток, где каждая попытка стоит argon2.

    Выдуманный логин не тратит `staff_login_ip` (#486): у отеля один внешний
    адрес на стойку, персонал и гостевой Wi-Fi, и шестьдесят выдуманных
    логинов запирали все новые входы персонала. Теперь аноним, не знающий
    логинов, упирается только в просторный `staff_login_ip_flood`.

    Проверяются ДО пароля все три, одинаково для любого логина: ответ по-прежнему
    не говорит, есть ли такой логин (spec 0037 §3). Различается лишь то, что
    списывается после отказа; чтобы этим воспользоваться, нужно самому подвести
    `staff_login_ip` к краю отказами по настоящим логинам — то есть уже знать их,
    и каждый такой бит стоит запертого адреса на окно лимита.

    Ключа «учётка+IP» нет: он строго слабее ключа учётки, который и так
    считает попытки со всех адресов, и подбор с ротацией адресов не ловит.

    `device_key` — браузер знаком этому логину (`staff_devices.known_device_key`,
    issue #402): тогда ключ один, `staff_login_device` на пару «устройство +
    учётка» с бюджетом учётки. Ключи учётки и IP гость исчерпывает сам — код
    отеля и логин с бейджа публичны, адрес у отеля с гостевым Wi-Fi общий, — а
    токен знакомого устройства выдаётся только после верного пароля этому
    логину: подобрать с него чужой пароль нельзя, свой — не быстрее ключа учётки.
    """
    settings = get_settings()
    if device_key is not None:
        return (
            _LoginBudget(
                "staff_login_device", device_key, settings.staff_login_rate_limit_attempts, True
            ),
        )
    account_key = hashlib.sha256(password_external_id(tenant_id, login).encode()).hexdigest()
    return (
        _LoginBudget(
            "staff_login_account", account_key, settings.staff_login_rate_limit_attempts, True
        ),
        _LoginBudget(
            "staff_login_ip", client_ip, settings.staff_login_ip_rate_limit_attempts, False
        ),
        _LoginBudget(
            "staff_login_ip_flood",
            client_ip,
            settings.staff_login_ip_flood_rate_limit_attempts,
            True,
        ),
    )


async def enforce_login_rate_limit(
    tenant_id: uuid.UUID, login: str, client_ip: str, *, device_key: str | None = None
) -> None:
    """Не исчерпан ли бюджет попыток — ДО проверки пароля (канон 0023, §3.3).

    Бюджет здесь только читается: тратит его одна `record_failed_login`, и
    только неудачная попытка (issue #207). Иначе успешные входы съедали
    общий IP-бюджет отеля, и день раздачи доступов умирал на одиннадцатом
    сотруднике, а утренний заступ смены получал «слишком много попыток» без
    единой ошибки пароля.

    Двери, где доказывается пароль, две — `staff_auth.login` и повторный ввод
    `staff_passkeys.require_password` (ADR-008 §8), обе под этими бюджетами:
    принятие приглашения всегда заводит новую учётку и чужой пароль не
    проверяет (spec 0037 §4), поэтому обходной двери в обход лимита нет.

    `device_key` — знакомое устройство (`_login_budgets`); без него — строгий
    путь всех трёх ключей, поэтому дверь, не знающая устройств (вход
    суперадмина), его не передаёт."""
    window_seconds = get_settings().staff_login_rate_limit_window_seconds
    for budget in _login_budgets(tenant_id, login, client_ip, device_key):
        if budget.limit <= 0:
            continue
        decision = await peek_rate_limit(
            budget.scope, budget.key, limit=budget.limit, window_seconds=window_seconds
        )
        # Fail-open при недоступном Redis — канон 0023 (стойкость держит argon2).
        if decision.available and not decision.allowed:
            logger.warning(
                "staff.login_rate_limited",
                scope=budget.scope,
                count=decision.count,
                limit=decision.limit,
            )
            record_staff_login("rate_limited")
            raise AppError(
                code=ERR_AUTH_LOGIN_RATE_LIMITED,
                message="Too many login attempts — try again later",
                status_code=429,
            )


async def record_failed_login(
    tenant_id: uuid.UUID,
    login: str,
    client_ip: str,
    *,
    known_login: bool,
    device_key: str | None = None,
) -> None:
    """Списать неудачную попытку с бюджетов входа (issue #207, #486).

    Зовётся ровно там, где отказ означает недоказанный пароль (ERR-AUTH-001):
    неизвестный логин, неверный пароль. Отказ доказавшему пароль — например,
    деактивированному сотруднику (ERR-AUTH-005) — попыткой подбора не
    является и бюджет не тратит: иначе уволенный сотрудник, чей телефон сам
    повторяет вход, выжигал бы лимит живой смене за тем же NAT.

    `known_login` — есть ли такой логин в отеле: отказ по выдуманному не
    тратит `staff_login_ip` (см. `_login_budgets`). Аргумент обязателен, чтобы
    новая дверь входа не выбрала ключи молча. `device_key` — тот же, что
    получила `enforce_login_rate_limit`: отказ со знакомого устройства тратит
    только его ключ.
    """
    window_seconds = get_settings().staff_login_rate_limit_window_seconds
    for budget in _login_budgets(tenant_id, login, client_ip, device_key):
        if budget.limit <= 0 or not (known_login or budget.spent_by_unknown_login):
            continue
        await consume_rate_limit(
            budget.scope, budget.key, limit=budget.limit, window_seconds=window_seconds
        )
