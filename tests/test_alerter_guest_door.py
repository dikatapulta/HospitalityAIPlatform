"""Гости упираются в лимит у входа в чат — восьмая линия алертера (issue #415, ERR-OPS-014).

Машина состояний — на снимках, собранных руками (помощники ``tests/test_alerter.py``);
тракт «/metrics → парсер → машина → отправка» — через ``run_alerter`` на MockTransport.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import httpx
import pytest

from hospitality.shared.config import get_settings
from hospitality.shared.metrics import record_guest_rate_limited
from hospitality.shared.tenancy import tenant_context
from hospitality.tools.alerter import run_alerter
from hospitality.tools.alerter_guest_door import (
    ERR_GUEST_DOOR_RATE_LIMITED,
    read_guest_door_rejections,
)
from tests.test_alerter import (
    SAMPLE_METRICS,
    TENANT_A,
    TENANT_B,
    FakeStagingStack,
    make_monitor,
    ready_probe,
)

# Две двери у двух отелей плюс лимиты чата, которые линия обязана не видеть.
DOOR_METRICS = f"""\
# HELP guest_rate_limited_total Отклонённые rate-limit'ом сообщения гостевого чата
# TYPE guest_rate_limited_total counter
guest_rate_limited_total{{tenant_id="{TENANT_A}",scope="guest_bind_link_consume"}} 2.0
guest_rate_limited_total{{tenant_id="{TENANT_B}",scope="guest_bind_link_consume"}} 1.0
guest_rate_limited_total{{tenant_id="{TENANT_A}",scope="guest_code_verify_ip"}} 4.0
guest_rate_limited_total{{tenant_id="{TENANT_A}",scope="guest_chat_window"}} 50.0
guest_rate_limited_total{{tenant_id="{TENANT_A}",scope="guest_message_length"}} 7.0
# HELP guest_rate_limited_created Отклонённые rate-limit'ом сообщения гостевого чата
# TYPE guest_rate_limited_created gauge
guest_rate_limited_created{{tenant_id="{TENANT_A}",scope="guest_bind_link_consume"}} 1.7e+09
"""

_ZERO = {
    "guest_bind_link_consume": 0.0,
    "guest_code_verify_ip": 0.0,
    "guest_code_verify": 0.0,
    "guest_code_verify_telegram": 0.0,
}


def doors(
    bind: float = 0.0, code_ip: float = 0.0, code_room: float = 0.0, code_chat: float = 0.0
) -> dict[str, float]:
    return {
        "guest_bind_link_consume": bind,
        "guest_code_verify_ip": code_ip,
        "guest_code_verify": code_room,
        "guest_code_verify_telegram": code_chat,
    }


def test_parser_sums_doors_over_tenants_and_ignores_chat_limits() -> None:
    assert read_guest_door_rejections(DOOR_METRICS) == doors(bind=3.0, code_ip=4.0)
    # Счётчик с метками до первого отказа в выдаче не появляется — это ноль.
    assert read_guest_door_rejections(SAMPLE_METRICS) == _ZERO


def test_parser_understands_real_exposition_format() -> None:
    """Парсер и выдача prometheus_client не должны разойтись молча."""
    from prometheus_client import generate_latest

    before = read_guest_door_rejections(generate_latest().decode())
    with tenant_context(uuid.UUID(TENANT_A)):
        record_guest_rate_limited("guest_bind_link_consume")
        record_guest_rate_limited("guest_chat_window")
    after = read_guest_door_rejections(generate_latest().decode())

    assert after["guest_bind_link_consume"] == before["guest_bind_link_consume"] + 1
    assert after["guest_code_verify_ip"] == before["guest_code_verify_ip"]


def test_door_rejection_alerts_after_baseline_and_respects_cooldown() -> None:
    monitor = make_monitor()

    def step(rejections: dict[str, float] | None, now: float) -> list[str]:
        return monitor.evaluate(ready_probe(ok=True, guest_door_rejections=rejections), now=now)

    assert step(doors(bind=5.0), now=0.0) == []  # базовая линия: отказы до старта алертера
    assert step(doors(bind=5.0), now=60.0) == []  # прироста нет
    wall = step(doors(bind=8.0, code_ip=1.0), now=120.0)
    during_cooldown = step(doors(bind=20.0, code_ip=1.0), now=180.0)
    after_cooldown = step(doors(bind=21.0, code_ip=1.0), now=1100.0)

    assert len(wall) == 1 and ERR_GUEST_DOOR_RATE_LIMITED in wall[0] and "🔴" in wall[0]
    assert "+4 отказа(ов) 429" in wall[0]
    assert "ссылка с листка — 3" in wall[0] and "лимит адреса — 1" in wall[0]
    assert "лимит комнаты" not in wall[0]  # дверь без отказов не называется
    assert during_cooldown == []
    assert len(after_cooldown) == 1 and "+1 отказа(ов) 429" in after_cooldown[0]


def test_door_line_survives_counter_reset_and_missing_metrics() -> None:
    """Рестарт приложения обнуляет счётчик — накопленное с нуля и есть прирост;
    недоступный /metrics — «не знаю»: базовая линия не теряется."""
    monitor = make_monitor()

    def step(rejections: dict[str, float] | None, now: float) -> list[str]:
        return monitor.evaluate(ready_probe(ok=True, guest_door_rejections=rejections), now=now)

    step(doors(code_room=10.0), now=0.0)
    assert step(None, now=60.0) == []
    assert step(doors(code_room=10.0), now=120.0) == []
    after_restart = step(doors(code_room=2.0), now=180.0)

    assert len(after_restart) == 1 and "лимит комнаты — 2" in after_restart[0]


def test_door_threshold_filters_and_zero_switches_line_off() -> None:
    def alerts(threshold: int, rejections: dict[str, float]) -> list[str]:
        monitor = make_monitor(guest_door_threshold=threshold)
        monitor.evaluate(ready_probe(ok=True, guest_door_rejections=_ZERO), now=0.0)
        return monitor.evaluate(ready_probe(ok=True, guest_door_rejections=rejections), now=60.0)

    assert alerts(3, doors(bind=2.0)) == []
    assert len(alerts(3, doors(bind=2.0, code_ip=1.0))) == 1
    assert alerts(0, doors(bind=100.0)) == []


@pytest.fixture
def alerter_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TELEGRAM_ALERT_BOT_TOKEN", "alert-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "-100777")
    monkeypatch.setenv("ALERT_POLL_INTERVAL_SECONDS", "1e-6")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_run_alerter_reports_door_rejections_from_metrics(alerter_settings: None) -> None:
    """Тракт «HTTP-ответ → парсер → машина → отправка»: тесты машины собирают
    ProbeResult руками, поэтому имя метрики и строка проводки в
    probe_application проверяются здесь. Первый опрос — базовая линия, второй
    приносит отказы у двери и обязан дать ровно один алерт."""
    stack = FakeStagingStack(ready_statuses=[200, 200])
    snapshots = [SAMPLE_METRICS, SAMPLE_METRICS + DOOR_METRICS]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/metrics":
            return httpx.Response(200, text=snapshots.pop(0))
        return stack.handler(request)

    run_alerter(iterations=2, transport=httpx.MockTransport(handler))

    texts = [message["text"] for message in stack.sent_messages]
    assert len(texts) == 1, texts
    assert ERR_GUEST_DOOR_RATE_LIMITED in texts[0] and "+7 отказа(ов) 429" in texts[0]
