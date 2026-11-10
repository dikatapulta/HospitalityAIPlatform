"""Проброс лимитов попыток в staging-стек (issue #486 п. 3).

Рунбук велит поднять лимит строкой в `.env`, а до контейнера она доезжает только
через `environment:` сервиса app. Умолчание там — второй экземпляр числа из
`shared/config.py`; тест держит их равными, чтобы правка лимита в коде не
оставила staging на старом значении молча.
"""

from __future__ import annotations

import re
from pathlib import Path

from hospitality.shared.config import Settings

_COMPOSE = Path(__file__).resolve().parent.parent / "ops/deploy/docker-compose.staging.yml"
_PASS_THROUGH = re.compile(r"^\s+(\w*RATE_LIMIT\w*): \$\{(\w+):-([^}]*)\}\s*$", re.MULTILINE)

# Лимиты, которые рунбук errors.md называет рычагом (ERR-AUTH-006, ERR-AUTH-019, ERR-WEB-004).
_LEVERS = {
    "STAFF_LOGIN_RATE_LIMIT_ATTEMPTS",
    "STAFF_LOGIN_IP_RATE_LIMIT_ATTEMPTS",
    "STAFF_LOGIN_IP_FLOOD_RATE_LIMIT_ATTEMPTS",
    "STAFF_LOGIN_RATE_LIMIT_WINDOW_SECONDS",
    "GUEST_CODE_VERIFY_RATE_LIMIT_ATTEMPTS",
    "GUEST_CODE_VERIFY_RATE_LIMIT_WINDOW_SECONDS",
    "GUEST_CODE_VERIFY_IP_RATE_LIMIT_ATTEMPTS",
    "GUEST_BIND_LINK_CONSUME_RATE_LIMIT_ATTEMPTS",
    "GUEST_BIND_LINK_CONSUME_RATE_LIMIT_WINDOW_SECONDS",
    "STAFF_INVITE_ISSUE_RATE_LIMIT_ATTEMPTS",
    "STAFF_INVITE_ISSUE_RATE_LIMIT_WINDOW_SECONDS",
}


def test_rate_limit_levers_reach_app_with_code_defaults() -> None:
    found = {
        name: (source, default)
        for name, source, default in _PASS_THROUGH.findall(_COMPOSE.read_text(encoding="utf-8"))
    }

    assert found.keys() >= _LEVERS, sorted(_LEVERS - found.keys())
    for name, (source, default) in found.items():
        assert source == name, f"{name} берёт значение из чужой переменной {source}"
        field = Settings.model_fields[name.lower()]
        assert str(field.default) == default, f"{name}: compose {default}, код {field.default}"
