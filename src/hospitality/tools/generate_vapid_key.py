"""Новый ключ VAPID для веб-пуша кабинета (spec 0050 §8) — CLI.

Печатает приватный ключ — значение `STAFF_PUSH_VAPID_PRIVATE_KEY` — и,
отдельной строкой, публичный: его ничего не требует, он для сверки (тот же
ключ кабинет отдаёт телефонам). Запускать на сервере, внутри контейнера, чтобы
ключ не ходил по чатам и почте (рунбук `docs/runbooks/staff-push.md`):

    docker compose -f /opt/hospitality/docker-compose.staging.yml exec app \\
        python -m hospitality.tools.generate_vapid_key

Ключ — 32 случайных байта кривой P-256 в base64url. Смена ключа обрывает все
подписки телефонов: они переподписываются сами при открытии кабинета.
"""

from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from hospitality.shared.ports.web_push import b64url_encode


def generate_vapid_key() -> tuple[str, str]:
    """Пара (приватный, публичный) в base64url — форма `STAFF_PUSH_VAPID_PRIVATE_KEY`."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_value = private_key.private_numbers().private_value.to_bytes(32, "big")
    public_point = private_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return b64url_encode(private_value), b64url_encode(public_point)


def main() -> None:
    private_key, public_key = generate_vapid_key()
    print(f"STAFF_PUSH_VAPID_PRIVATE_KEY={private_key}")
    print(f"# публичный ключ (для сверки, в .env не нужен): {public_key}")


if __name__ == "__main__":  # pragma: no cover — точка входа процесса
    main()
