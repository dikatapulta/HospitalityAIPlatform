"""TOTP своими силами (spec 0044 §9): векторы RFC 6238, окно, повтор, формат."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from hospitality.platform.totp import (
    DIGITS,
    STEP_SECONDS,
    code_at,
    generate_secret,
    grouped_secret,
    matching_step,
    provisioning_uri,
    time_step,
)

# Приложение B RFC 6238: ключ SHA-1 — ASCII «12345678901234567890».
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode().rstrip("=")


@pytest.mark.parametrize(
    ("unix_time", "expected"),
    [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    ],
)
def test_code_matches_rfc6238_vectors(unix_time: int, expected: str) -> None:
    step = time_step(datetime.fromtimestamp(unix_time, tz=UTC))
    assert code_at(RFC_SECRET, step, digits=8) == expected
    # Шесть цифр — те же младшие разряды того же усечённого числа.
    assert code_at(RFC_SECRET, step) == expected[-DIGITS:]


def test_secret_is_160_bits_base32_without_padding() -> None:
    secret = generate_secret()
    assert len(secret) == 32 and "=" not in secret
    assert len(base64.b32decode(secret)) == 20
    assert generate_secret() != secret


def test_window_accepts_neighbour_steps_only() -> None:
    now = datetime(2026, 10, 24, 12, 0, 15, tzinfo=UTC)
    secret = generate_secret()
    current = time_step(now)
    for shift in (-1, 0, 1):
        code = code_at(secret, current + shift)
        assert matching_step(secret, code, now=now, last_used_step=None) == current + shift
    for shift in (-2, 2):
        code = code_at(secret, current + shift)
        assert matching_step(secret, code, now=now, last_used_step=None) is None


def test_replay_of_used_or_older_step_is_rejected() -> None:
    now = datetime(2026, 10, 24, 12, 0, 15, tzinfo=UTC)
    secret = generate_secret()
    current = time_step(now)
    code = code_at(secret, current)
    assert matching_step(secret, code, now=now, last_used_step=current) is None
    assert (
        matching_step(secret, code_at(secret, current - 1), now=now, last_used_step=current) is None
    )
    later = now + timedelta(seconds=STEP_SECONDS)
    assert matching_step(
        secret, code_at(secret, current + 1), now=later, last_used_step=current
    ) == (current + 1)


@pytest.mark.parametrize("raw", ["", "12345", "1234567", "abcdef", "١٢٣٤٥٦", "12-345"])
def test_malformed_code_never_matches(raw: str) -> None:
    now = datetime(2026, 10, 24, 12, 0, 15, tzinfo=UTC)
    assert matching_step(generate_secret(), raw, now=now, last_used_step=None) is None


def test_spaces_inside_code_are_forgiven() -> None:
    now = datetime(2026, 10, 24, 12, 0, 15, tzinfo=UTC)
    secret = generate_secret()
    code = code_at(secret, time_step(now))
    spaced = f" {code[:3]} {code[3:]} "
    assert matching_step(secret, spaced, now=now, last_used_step=None) == time_step(now)


def test_provisioning_uri_is_what_authenticator_apps_read() -> None:
    secret = generate_secret()
    uri = provisioning_uri(secret, account="ADMN (demo-hotel)")
    parts = urlsplit(uri)
    assert parts.scheme == "otpauth" and parts.netloc == "totp"
    assert parts.path == "/Necturn%3AADMN%20%28demo-hotel%29"
    query = parse_qs(parts.query)
    assert query == {
        "secret": [secret],
        "issuer": ["Necturn"],
        "algorithm": ["SHA1"],
        "digits": ["6"],
        "period": ["30"],
    }


def test_grouped_secret_for_manual_entry() -> None:
    assert grouped_secret("ABCDEFGHIJ") == "ABCD EFGH IJ"
