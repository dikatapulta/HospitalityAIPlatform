"""Task 0007: валидация LOG_LEVEL в Settings — опечатка в конфигурации падает
внятной ошибкой на старте, а не ValueError из глубин logging (crash-loop)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hospitality.shared.config import Settings


def test_invalid_log_level_is_rejected_with_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "verbose")

    with pytest.raises(ValidationError) as exc_info:
        Settings()

    # В сообщении видно и поле, и допустимые значения — диагноз без чтения кода.
    assert "log_level" in str(exc_info.value)
    assert "'DEBUG', 'INFO', 'WARNING' or 'ERROR'" in str(exc_info.value)


@pytest.mark.parametrize("raw", ["debug", " INFO ", "Warning"])
def test_log_level_tolerates_case_and_whitespace(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("LOG_LEVEL", raw)

    assert Settings().log_level == raw.strip().upper()


def test_trusted_proxy_ips_are_parsed_from_comma_separated_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #207: список прокси в env — через запятую, а не JSON-массивом."""
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "172.16.0.0/12, 127.0.0.1")

    assert [str(network) for network in Settings().trusted_proxy_ips] == [
        "172.16.0.0/12",
        "127.0.0.1/32",
    ]


def test_invalid_trusted_proxy_ip_is_rejected_on_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Опечатка в CIDR обязана падать на старте: молча она вернула бы дефект
    #207 — весь отель в одном ключе rate-limit, и заметить это нечем."""
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "172.16.0.0/44")

    with pytest.raises(ValidationError) as exc_info:
        Settings()

    assert "trusted_proxy_ips" in str(exc_info.value)
