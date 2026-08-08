"""Настоящий адрес клиента за доверенным прокси (issue #207, shared/clientip.py).

Без этого разбора весь отель за Cloudflare-туннелем приходит с одного адреса
(соседний контейнер), и лимит попыток входа по IP становится общим на всех.
С разбором цена ошибки обратная: доверие не тому — обход любого лимита по
адресу, поэтому проверка соседа по сокету покрыта отдельно.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import Request

from hospitality.shared.clientip import CLIENT_IP_HEADER, UNKNOWN_CLIENT_IP, client_ip
from hospitality.shared.config import get_settings

PROXY = "172.18.0.7"  # адрес cloudflared внутри docker-сети
GUEST = "203.0.113.9"  # адрес сотрудника в интернете


def _request(*, peer: str | None, headers: dict[str, str] | None = None) -> Request:
    """Запрос без сети: ASGI-scope — весь вход `client_ip`."""
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/staff/",
            "headers": [
                (name.lower().encode(), value.encode()) for name, value in (headers or {}).items()
            ],
            "client": (peer, 44321) if peer is not None else None,
        }
    )


@pytest.fixture
def trusted_proxies(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "172.16.0.0/12")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_header_from_trusted_proxy_wins(trusted_proxies: None) -> None:
    request = _request(peer=PROXY, headers={CLIENT_IP_HEADER: GUEST})

    assert client_ip(request) == GUEST


def test_header_from_untrusted_peer_is_ignored(trusted_proxies: None) -> None:
    """Подделка заголовка не должна давать чужой ключ лимита."""
    request = _request(peer="198.51.100.4", headers={CLIENT_IP_HEADER: "10.0.0.1"})

    assert client_ip(request) == "198.51.100.4"


def test_header_ignored_when_nobody_is_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Дефолт (пустой TRUSTED_PROXY_IPS) — верим только сокету."""
    monkeypatch.delenv("TRUSTED_PROXY_IPS", raising=False)
    get_settings.cache_clear()
    try:
        request = _request(peer=PROXY, headers={CLIENT_IP_HEADER: GUEST})

        assert client_ip(request) == PROXY
    finally:
        get_settings.cache_clear()


def test_malformed_header_falls_back_to_socket(trusted_proxies: None) -> None:
    """Мусор из заголовка не попадает в ключ Redis (он бы исказил ключ)."""
    request = _request(peer=PROXY, headers={CLIENT_IP_HEADER: "not-an-ip:*"})

    assert client_ip(request) == PROXY


def test_no_header_uses_socket(trusted_proxies: None) -> None:
    assert client_ip(_request(peer=PROXY)) == PROXY


def test_no_socket_is_unknown(trusted_proxies: None) -> None:
    """ASGI-транспорт тестов сокета не открывает — ключ обязан быть непустым."""
    assert client_ip(_request(peer=None)) == UNKNOWN_CLIENT_IP
