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
import string
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
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
ERR_AUTH_PASSWORD_GUESSABLE = "ERR-AUTH-016"

# Формат логина отеля (spec 0037 §2) — после нормализации (trim + upper).
# Кириллицы нет намеренно: `В`, `О`, `М` неотличимы от латинских на экране.
LOGIN_PATTERN: Final = re.compile(r"[A-Z][A-Z0-9]{2,11}")

# Политика пароля (issue #486): длина и «не угадывается первыми попытками».
# Лимит учётки — 10 отказов за 10 минут, то есть 1440 попыток в сутки по логину,
# который подсказывает открытый алгоритм (#402); подбор начинают со списков частых
# паролей, рядов клавиатуры и того, что видно о человеке. Без zxcvbn-оценок (P-1):
# только то, что угадывается первым.
PASSWORD_MIN_LENGTH: Final = 8

# Частые пароли: источник, отбор и лицензия — в шапке файла.
_COMMON_PASSWORDS: Final = frozenset(
    line
    for line in Path(__file__)
    .with_name("common_passwords.txt")
    .read_text(encoding="utf-8")
    .splitlines()
    if line and not line.startswith("#")
)

# Ряды клавиатуры (латиница и йцукен) и алфавиты; кусок ряда в любую сторону —
# «12345678», «87654321», «qwertyui», «йцукенгш», «abcd1234».
_KEYBOARD_AND_ALPHABET_RUNS: Final = (
    "01234567890",
    "abcdefghijklmnopqrstuvwxyz",
    "qwertyuiop",
    "asdfghjkl",
    "zxcvbnm",
    "йцукенгшщзхъ",
    "фывапролджэ",
    "ячсмитьбю",
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
)

# «Слово в середине» — пароль без цифр и знаков по краям: `Password1!` → `password`,
# `qwerty123` → `qwerty`. Короче четырёх знаков слово не проверяется.
_EDGE_CHARS: Final = string.digits + string.punctuation + " "
_MIN_WORD_LENGTH: Final = 4

# Часть логина или имени короче — слишком часто стоит внутри чужих слов.
_MIN_PERSONAL_PART_LENGTH: Final = 3

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


def _is_run(fragment: str) -> bool:
    return any(fragment in run or fragment in run[::-1] for run in _KEYBOARD_AND_ALPHABET_RUNS)


def _is_repetition(folded: str) -> bool:
    """Один кусок несколько раз: `BORMBORM`, `12121212`, `aaaaaaaa`. Строка —
    повтор куска ровно тогда, когда находится внутри себя удвоенной без краёв;
    поиск линейный, а длину пароля сверху ничто не ограничивает."""
    return folded in (folded + folded)[1:-1]


def _personal_parts(personal: Iterable[str]) -> set[str]:
    parts: set[str] = set()
    for value in personal:
        parts.update(
            part
            for part in re.split(r"\W+", value.casefold())
            if len(part) >= _MIN_PERSONAL_PART_LENGTH
        )
    return parts


def _is_guessable(password: str, personal: Iterable[str]) -> bool:
    folded = password.casefold()
    word = folded.strip(_EDGE_CHARS)
    fragments = (folded, word) if len(word) >= _MIN_WORD_LENGTH else (folded,)
    return (
        any(fragment in _COMMON_PASSWORDS or _is_run(fragment) for fragment in fragments)
        or _is_repetition(folded)
        or any(part in folded for part in _personal_parts(personal))
    )


def ensure_password_policy(password: str, *, personal: Iterable[str]) -> None:
    """Единственный источник ERR-AUTH-007 и ERR-AUTH-016 (P-12).

    ERR-AUTH-007 — короче минимума; ERR-AUTH-016 — угадывается первыми
    попытками: частый пароль (целиком или «словом в середине»), ряд клавиатуры
    или алфавита, повтор одного куска, логин или часть имени внутри.

    `personal` — логин и имя того, кому задаётся пароль: оба видны коллегам и
    подсказываются алгоритмом логина (#402). Аргумент обязателен, чтобы новая
    дверь не пропустила проверку молча.

    Зовётся ДО argon2 и до записи в БД: отказ ничего не меняет. Дверь по
    ссылке (приглашение, новый пароль) берёт логин и имя из той же ссылки, что
    уже показана её держателю, — недействительная ссылка отвечает своим отказом
    раньше политики при любом пароле, и отказ политики не говорит ничего сверх
    страницы (урок блокера ревью PR #159: исход не должен зависеть от того,
    что лежит в базе и не видно держателю).
    """
    if len(password) < PASSWORD_MIN_LENGTH:
        raise AppError(
            code=ERR_AUTH_PASSWORD_TOO_SHORT,
            message=f"Password must be at least {PASSWORD_MIN_LENGTH} characters long",
            status_code=422,
        )
    if _is_guessable(password, personal):
        logger.info("staff.password_rejected_guessable")
        raise AppError(
            code=ERR_AUTH_PASSWORD_GUESSABLE,
            message=(
                "Password is too easy to guess: no common passwords, keyboard runs, "
                "repeats, login or name"
            ),
            status_code=422,
        )


async def hash_password(password: str, *, personal: Iterable[str]) -> str:
    """argon2id-хэш пароля; сначала политика (ERR-AUTH-007, ERR-AUTH-016)."""
    ensure_password_policy(password, personal=personal)
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

    Дверь, где доказывается пароль, одна — `staff_auth.login`: принятие
    приглашения всегда заводит новую учётку и чужой пароль не проверяет
    (spec 0037 §4), поэтому обходной двери в обход лимита нет.

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
