"""Пределы памяти и процессора у каждого сервиса staging-стека (issue #1053 п. 2).

Compose-файл один у staging и гостевой копии отеля на общем сервере. Сервис без
предела — дыра: его утечка съедает память сервера, и ядро убивает чужой процесс,
хоть базу копии. Процессор сверх числа ядер сервера docker отвергает, и деплой
падает, — поэтому умолчание не больше одного ядра. Ручка `.env` из compose
названа в `.env.staging.example`: рунбук велит поднять её там.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_DEPLOY = Path(__file__).resolve().parent.parent / "ops/deploy"
_COMPOSE: dict[str, Any] = yaml.safe_load(
    (_DEPLOY / "docker-compose.staging.yml").read_text(encoding="utf-8")
)
_SERVICES: dict[str, dict[str, Any]] = _COMPOSE["services"]
_KNOB = re.compile(r"^\$\{([A-Z][A-Z0-9_]*):-([^}]+)\}$")
_MEMORY = re.compile(r"^\d+[mg]$")


def _split(value: object) -> tuple[str | None, str]:
    """`${NAME:-умолчание}` → (NAME, умолчание); число как есть → (None, число)."""
    text = str(value)
    match = _KNOB.match(text)
    if match:
        return match.group(1), match.group(2)
    assert "$" not in text, f"ручка без умолчания: {text}"
    return None, text


@pytest.mark.parametrize("name", sorted(_SERVICES))
def test_service_has_memory_and_cpu_limit(name: str) -> None:
    service = _SERVICES[name]
    assert "mem_limit" in service, f"{name}: нет mem_limit"
    assert "cpus" in service, f"{name}: нет cpus"
    _, memory = _split(service["mem_limit"])
    assert _MEMORY.match(memory), f"{name}: память {memory!r} — вида 1g или 512m"
    _, cpus = _split(service["cpus"])
    assert 0 < float(cpus) <= 1.0, f"{name}: процессор {cpus} — больше одного ядра"


def test_compose_knobs_are_named_in_staging_env_example() -> None:
    example = (_DEPLOY / ".env.staging.example").read_text(encoding="utf-8")
    knobs = {
        knob
        for service in _SERVICES.values()
        for key in ("mem_limit", "cpus")
        if (knob := _split(service[key])[0]) is not None
    }
    assert knobs, "ручек нет — тест проверяет пустоту"
    missing = sorted(k for k in knobs if not re.search(rf"^# {k}=$", example, re.MULTILINE))
    assert not missing, f"нет строки `# NAME=` в .env.staging.example: {missing}"
