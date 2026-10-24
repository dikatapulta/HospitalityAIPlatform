"""Одноразовые коды из приложения (TOTP, RFC 6238) — второй фактор суперадмина.

Spec 0044 §3, §9. Своими силами на stdlib, без зависимости: код — HMAC-SHA1 от
номера 30-секундного шага, динамическое усечение RFC 4226 §5.3, 6 цифр. Это
понимают Google Authenticator и аналоги. Тесты — по векторам приложения B
RFC 6238 (`tests/test_totp.py`).

Принимаются текущий шаг и соседние (`DRIFT_STEPS`): часы телефона расходятся
с сервером. Повтор кода отсекает вызывающий: `matching_step` возвращает номер
шага, и шаг не больше последнего принятого не подходит (иначе подсмотренный
код работал бы ещё минуту).

Секрет — 160 бит (рекомендация RFC 4226 §4), base32 без «=» — так его
принимают приложения и так он вводится руками. Хранение и почему открытым —
spec 0044 §7. Ни секрет, ни код в логи не пишутся.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime
from typing import Final
from urllib.parse import quote, urlencode

SECRET_BYTES: Final = 20
DIGITS: Final = 6
STEP_SECONDS: Final = 30
DRIFT_STEPS: Final = 1
# Подпись учётки в приложении на телефоне: «Necturn: ADMN (код отеля)».
ISSUER: Final = "Necturn"


def generate_secret() -> str:
    """Новый секрет: 160 случайных бит в base32 (32 знака, без «=»)."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode().rstrip("=")


def time_step(moment: datetime) -> int:
    """Номер 30-секундного шага от начала эпохи Unix (RFC 6238 §4)."""
    return int(moment.timestamp()) // STEP_SECONDS


def code_at(secret: str, step: int, *, digits: int = DIGITS) -> str:
    """Код шага: HMAC-SHA1(секрет, шаг) → динамическое усечение → `digits` цифр."""
    padded = secret.upper() + "=" * (-len(secret) % 8)
    key = base64.b32decode(padded)
    digest = hmac.new(key, step.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return str(value % 10**digits).zfill(digits)


def matching_step(
    secret: str, code: str, *, now: datetime, last_used_step: int | None
) -> int | None:
    """Шаг, которому соответствует код, или None.

    Пробелы внутри кода прощаются (приложения показывают «123 456»). Шаги
    окна перебираются все, без раннего выхода, сравнение — постоянного
    времени. Шаг не больше `last_used_step` — повтор, не подходит.
    """
    normalized = "".join(code.split())
    if len(normalized) != DIGITS or not (normalized.isascii() and normalized.isdigit()):
        return None
    current = time_step(now)
    found: int | None = None
    for step in range(current - DRIFT_STEPS, current + DRIFT_STEPS + 1):
        fresh = last_used_step is None or step > last_used_step
        if hmac.compare_digest(code_at(secret, step), normalized) and fresh:
            found = step
    return found


def provisioning_uri(secret: str, *, account: str) -> str:
    """Адрес `otpauth://` для QR: так приложение заводит учётку одним снимком."""
    label = quote(f"{ISSUER}:{account}")
    query = urlencode(
        {
            "secret": secret,
            "issuer": ISSUER,
            "algorithm": "SHA1",
            "digits": DIGITS,
            "period": STEP_SECONDS,
        }
    )
    return f"otpauth://totp/{label}?{query}"


def grouped_secret(secret: str) -> str:
    """Секрет группами по четыре — для ручного ввода, если QR не сканируется."""
    return " ".join(secret[index : index + 4] for index in range(0, len(secret), 4))
