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
