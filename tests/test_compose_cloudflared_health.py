"""Healthcheck туннеля в staging-стеке смотрит туда, где слушает его сервер метрик (#407).

`cloudflared tunnel ready` без `--metrics` не работает («--metrics has to be provided»),
а с адресом, на котором туннель не слушает, отвечает отказом всегда: контейнер вечно
unhealthy, и `up --wait` в deploy.sh валит каждый деплой обоих контуров. Адрес стоит
в compose дважды — тест держит экземпляры равными.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_COMPOSE = Path(__file__).resolve().parent.parent / "ops/deploy/docker-compose.staging.yml"


def _metrics_address(argv: list[str]) -> str:
    assert "--metrics" in argv, f"нет --metrics в {argv}"
    return argv[argv.index("--metrics") + 1]


def test_cloudflared_healthcheck_asks_ready_on_its_own_metrics_address() -> None:
    compose: dict[str, Any] = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    service = compose["services"]["cloudflared"]
    command: list[str] = service["command"]
    test: list[str] = service["healthcheck"]["test"]

    assert test[0] == "CMD"
    assert test[1:3] == ["cloudflared", "tunnel"] and test[-1] == "ready", test
    assert command[0] == "tunnel" and command[-1] == "run", command
    assert _metrics_address(test) == _metrics_address(command)
