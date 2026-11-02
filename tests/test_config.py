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


@pytest.mark.parametrize(
    "raw", ["javascript:alert(1)", "http://hr.necturn.com", "https://", "hr.necturn.com"]
)
def test_demo_menu_link_must_be_https(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """Issue #628: адрес пункта-ссылки ложится в `href` меню кабинета —
    не `https://` с хостом, и старт падает, а не кладёт в меню `javascript:`."""
    monkeypatch.setenv("DEMO_MENTOR_URL", raw)

    with pytest.raises(ValidationError) as exc_info:
        Settings()

    assert "demo_mentor_url" in str(exc_info.value)


def test_demo_menu_links_default_to_empty_and_accept_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (Settings().demo_mentor_url, Settings().demo_codex_url) == ("", "")
    monkeypatch.setenv("DEMO_CODEX_URL", " https://standart.necturn.com ")

    assert Settings().demo_codex_url == "https://standart.necturn.com"


@pytest.mark.parametrize(
    ("app_image", "release"),
    [
        ("ghcr.io/owner/hospitality-app:0123abcd", "0123abcd"),
        ("hospitality-app:staging", "staging"),
        ("localhost:5000/hospitality-app", ""),  # порт реестра — не тег
        ("ghcr.io/owner/hospitality-app", ""),
        ("", ""),
    ],
)
def test_app_release_is_image_tag_without_slashes(app_image: str, release: str) -> None:
    """#411: версия кода — тег образа; «/» в имени релиза Sentry запрещён."""
    assert Settings(app_image=app_image).app_release == release
