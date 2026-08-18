"""Task 0018: алертер — машина состояний, парсер метрик, цикл (§10.8).

Машина состояний и парсер — чистые функции без сети; цикл гоняется на
httpx.MockTransport (ни одного реального запроса).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from hospitality.shared.config import get_settings
from hospitality.shared.metrics import record_http_request
from hospitality.tools.alerter import (
    ERR_BACKUP_STALE,
    ERR_ERROR_SPIKE,
    ERR_LLM_BUDGET_NEAR_LIMIT,
    ERR_OUTBOX_BACKLOG,
    ERR_READY_UNAVAILABLE,
    ERR_WORKER_STALLED,
    AlertMonitor,
    BackupProbe,
    ProbeResult,
    TenantBudgetUsage,
    probe_backups,
    read_gauge,
    read_llm_budget_usage,
    run_alerter,
    sum_server_errors,
)

# ---------------------------------------------------------------------------
# Парсер /metrics
# ---------------------------------------------------------------------------

SAMPLE_METRICS = """\
# HELP http_requests_total HTTP-запросы по маршрутам (RED, FOUNDATION §10.7)
# TYPE http_requests_total counter
http_requests_total{method="GET",route="/a",status="5xx"} 7.0
http_requests_total{method="GET",route="/a",status="2xx"} 3.0
http_requests_total{method="POST",route="/b",status="5xx"} 2.0
outbox_pending_events 4.0
worker_heartbeat_age_seconds 12.0
"""

# Та же выдача, но воркер молчит 15 минут, а очередь встала (issue #136):
# оба значения выше умолчаний Settings (300 с и 100 событий).
STALLED_WORKER_METRICS = """\
# HELP http_requests_total HTTP-запросы по маршрутам (RED, FOUNDATION §10.7)
# TYPE http_requests_total counter
http_requests_total{method="GET",route="/a",status="2xx"} 3.0
outbox_pending_events 500.0
worker_heartbeat_age_seconds 900.0
"""

TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"

# Приложение живо, но один отель сжёг 87% дневного лимита LLM (issue #103),
# второй — 3%: алерт обязан прийти ровно на первого и назвать его.
NEAR_BUDGET_METRICS = f"""\
# HELP llm_daily_spend_usd Расход LLM тенанта за текущие UTC-сутки в USD
# TYPE llm_daily_spend_usd gauge
llm_daily_spend_usd{{tenant_id="{TENANT_A}"}} 8.7
llm_daily_spend_usd{{tenant_id="{TENANT_B}"}} 0.3
# HELP llm_daily_budget_usd Дневной лимит LLM тенанта в USD
# TYPE llm_daily_budget_usd gauge
llm_daily_budget_usd{{tenant_id="{TENANT_A}"}} 10.0
llm_daily_budget_usd{{tenant_id="{TENANT_B}"}} 10.0
"""


def test_sum_server_errors_counts_only_5xx() -> None:
    assert sum_server_errors(SAMPLE_METRICS) == 9.0


def test_sum_server_errors_understands_real_exposition_format() -> None:
    """Парсер и выдача prometheus_client не должны разойтись молча."""
    from prometheus_client import generate_latest

    before = sum_server_errors(generate_latest().decode())
    record_http_request(
        method="GET", route="/alerter-parser-test", status_code=500, duration_seconds=0.01
    )
    after = sum_server_errors(generate_latest().decode())

    assert after == before + 1


# ---------------------------------------------------------------------------
# Машина состояний
# ---------------------------------------------------------------------------


# Порог свежести бэкапа в тестах — умолчание Settings (30 часов, issue #106).
_BACKUP_MAX_AGE_SECONDS = 30 * 3600.0
_HOUR = 3600.0


def backup_probe(
    *, age_hours: float | None = None, readable: bool = True, error: str = ""
) -> BackupProbe:
    return BackupProbe(
        directory="/backups",
        readable=readable,
        latest_age_seconds=None if age_hours is None else age_hours * _HOUR,
        error=error,
    )


def make_monitor(*, llm_budget_ratio: float = 0.8) -> AlertMonitor:
    return AlertMonitor(
        ready_failure_threshold=2,
        error_spike_threshold=5,
        cooldown_seconds=900.0,
        environment="test",
        runbook_url="https://example.invalid/alerts.md",
        worker_heartbeat_max_age_seconds=300.0,
        outbox_depth_threshold=100,
        llm_budget_ratio=llm_budget_ratio,
        backup_max_age_seconds=_BACKUP_MAX_AGE_SECONDS,
    )


def ready_probe(
    *,
    ok: bool,
    errors_total: float | None = 0.0,
    heartbeat_age: float | None = None,
    outbox_depth: float | None = None,
    llm_budget_usage: list[TenantBudgetUsage] | None = None,
    backup: BackupProbe | None = None,
) -> ProbeResult:
    return ProbeResult(
        ready_ok=ok,
        ready_detail='{"status": "unavailable"}' if not ok else '{"status": "ok"}',
        server_error_total=errors_total,
        worker_heartbeat_age_seconds=heartbeat_age,
        outbox_pending_events=outbox_depth,
        llm_budget_usage=llm_budget_usage or [],
        backup=backup,
    )


def budget_usage(spent: float, *, tenant: str = TENANT_A, budget: float = 10.0) -> ProbeResult:
    return ready_probe(
        ok=True,
        llm_budget_usage=[TenantBudgetUsage(tenant_id=tenant, spent_usd=spent, budget_usd=budget)],
    )


def test_ready_alert_fires_once_after_threshold_and_recovers() -> None:
    monitor = make_monitor()

    assert monitor.evaluate(ready_probe(ok=False), now=0.0) == []  # одиночный чих — не алерт
    second = monitor.evaluate(ready_probe(ok=False), now=60.0)
    third = monitor.evaluate(ready_probe(ok=False), now=120.0)
    recovery = monitor.evaluate(ready_probe(ok=True), now=180.0)

    assert len(second) == 1 and ERR_READY_UNAVAILABLE in second[0]
    assert "runbook" in second[0] and "tenant: platform" in second[0]
    assert third == []  # алерт уже активен — не спамим каждый опрос
    assert len(recovery) == 1 and "✅" in recovery[0]
    # Новый цикл падения после восстановления снова приводит к алерту.
    monitor.evaluate(ready_probe(ok=False), now=240.0)
    assert monitor.evaluate(ready_probe(ok=False), now=300.0) != []


def test_error_spike_alert_respects_baseline_and_cooldown() -> None:
    monitor = make_monitor()

    assert (
        monitor.evaluate(ready_probe(ok=True, errors_total=100.0), now=0.0) == []
    )  # базовая линия
    spike = monitor.evaluate(ready_probe(ok=True, errors_total=110.0), now=60.0)
    during_cooldown = monitor.evaluate(ready_probe(ok=True, errors_total=200.0), now=120.0)
    after_cooldown = monitor.evaluate(ready_probe(ok=True, errors_total=300.0), now=1200.0)

    assert len(spike) == 1 and ERR_ERROR_SPIKE in spike[0]
    assert during_cooldown == []
    assert len(after_cooldown) == 1


def test_error_spike_survives_counter_reset() -> None:
    """Перезапуск приложения обнуляет счётчики — отрицательная дельта не алертит
    ложно, а накопленное с нуля считается новой дельтой."""
    monitor = make_monitor()

    monitor.evaluate(ready_probe(ok=True, errors_total=100.0), now=0.0)
    small_after_reset = monitor.evaluate(ready_probe(ok=True, errors_total=2.0), now=60.0)
    big_after_reset = monitor.evaluate(ready_probe(ok=True, errors_total=20.0), now=120.0)

    assert small_after_reset == []
    assert len(big_after_reset) == 1


def test_unavailable_metrics_do_not_alert_and_keep_baseline() -> None:
    monitor = make_monitor()

    monitor.evaluate(ready_probe(ok=True, errors_total=100.0), now=0.0)
    unavailable = monitor.evaluate(ready_probe(ok=False, errors_total=None), now=60.0)
    recovered = monitor.evaluate(ready_probe(ok=True, errors_total=103.0), now=120.0)

    assert unavailable == []  # /metrics упал вместе с приложением — покроет ERR-OPS-001
    assert recovered == []  # базовая линия не потеряна: дельта 3 < порога


def test_stale_worker_heartbeat_alerts_once_and_recovers() -> None:
    """Issue #136: мёртвый воркер виден снаружи по возрасту его пульса."""
    monitor = make_monitor()

    fresh = monitor.evaluate(ready_probe(ok=True, heartbeat_age=12.0), now=0.0)
    stale = monitor.evaluate(ready_probe(ok=True, heartbeat_age=400.0), now=60.0)
    still_stale = monitor.evaluate(ready_probe(ok=True, heartbeat_age=460.0), now=120.0)
    recovery = monitor.evaluate(ready_probe(ok=True, heartbeat_age=5.0), now=180.0)

    assert fresh == []
    assert len(stale) == 1 and ERR_WORKER_STALLED in stale[0] and "🔴" in stale[0]
    assert "7 мин" in stale[0] and "порог 5 мин" in stale[0]
    assert still_stale == []  # алерт уже активен — не спамим каждую минуту
    assert len(recovery) == 1 and "✅" in recovery[0]


def test_missing_heartbeat_metric_does_not_alert() -> None:
    """Пустая метрика (NaN — БД недоступна, или /metrics не ответил) — это
    «не знаю», а не «воркер мёртв»: такой отказ покрывает ERR-OPS-001."""
    monitor = make_monitor()

    assert monitor.evaluate(ready_probe(ok=True, heartbeat_age=None), now=0.0) == []


def test_outbox_backlog_alerts_once_and_recovers() -> None:
    """Вторая линия того же симптома: пульс свежий, но очередь не разбирается."""
    monitor = make_monitor()

    normal = monitor.evaluate(ready_probe(ok=True, outbox_depth=3.0), now=0.0)
    backlog = monitor.evaluate(ready_probe(ok=True, outbox_depth=137.0), now=60.0)
    growing = monitor.evaluate(ready_probe(ok=True, outbox_depth=400.0), now=120.0)
    drained = monitor.evaluate(ready_probe(ok=True, outbox_depth=4.0), now=180.0)

    assert normal == []
    assert len(backlog) == 1 and ERR_OUTBOX_BACKLOG in backlog[0] and "137" in backlog[0]
    assert growing == []
    assert len(drained) == 1 and "✅" in drained[0]


def test_dead_worker_gives_both_lines_independently() -> None:
    """Мёртвый воркер даёт оба алерта: пульс стухает сразу, очередь копится
    следом — вторая линия не заменяет первую, а страхует её."""
    monitor = make_monitor()

    messages = monitor.evaluate(
        ready_probe(ok=True, heartbeat_age=900.0, outbox_depth=500.0), now=0.0
    )

    assert len(messages) == 2
    assert any(ERR_WORKER_STALLED in message for message in messages)
    assert any(ERR_OUTBOX_BACKLOG in message for message in messages)


def test_llm_budget_alert_fires_once_per_tenant_and_recovers() -> None:
    """Issue #103: о приближении к дневному лимиту говорят один раз, отбой —
    когда расход снова ниже порога (сменились UTC-сутки или подняли лимит)."""
    monitor = make_monitor()

    quiet = monitor.evaluate(budget_usage(4.0), now=0.0)
    crossed = monitor.evaluate(budget_usage(8.4), now=60.0)
    higher = monitor.evaluate(budget_usage(9.6), now=120.0)
    new_day = monitor.evaluate(budget_usage(0.1), now=180.0)

    assert quiet == []
    assert len(crossed) == 1
    assert ERR_LLM_BUDGET_NEAR_LIMIT in crossed[0] and "🔴" in crossed[0]
    assert "84%" in crossed[0] and "$8.40 из $10.00" in crossed[0]
    assert f"tenant: {TENANT_A}" in crossed[0]  # какой отель — видно из алерта
    assert higher == []  # алерт уже активен — не лента каждую минуту
    assert len(new_day) == 1 and "✅" in new_day[0]
    # Следующее приближение того же тенанта снова даёт алерт.
    assert len(monitor.evaluate(budget_usage(9.0), now=240.0)) == 1


def test_llm_budget_state_is_per_tenant() -> None:
    """У каждого отеля свой бюджет: алерт одного не глушит алерт другого."""
    monitor = make_monitor()

    first = monitor.evaluate(budget_usage(9.0, tenant=TENANT_A), now=0.0)
    second = monitor.evaluate(budget_usage(9.0, tenant=TENANT_B), now=60.0)

    assert len(first) == 1 and TENANT_A in first[0]
    assert len(second) == 1 and TENANT_B in second[0]


def test_llm_budget_without_data_keeps_state() -> None:
    """Снимка нет (БД недоступна, /metrics не ответил, старая версия) — это
    «не знаю»: алерт не гасится и не поднимается."""
    monitor = make_monitor()

    monitor.evaluate(budget_usage(9.0), now=0.0)
    blind = monitor.evaluate(ready_probe(ok=True), now=60.0)
    still_high = monitor.evaluate(budget_usage(9.5), now=120.0)

    assert blind == []
    assert still_high == []  # состояние пережило слепой опрос — повтора нет


def test_llm_budget_line_can_be_switched_off() -> None:
    """Страховочный люк: доля вне (0; 1) выключает линию целиком."""
    assert make_monitor(llm_budget_ratio=0.0).evaluate(budget_usage(9.9), now=0.0) == []
    assert make_monitor(llm_budget_ratio=1.0).evaluate(budget_usage(9.9), now=0.0) == []


def test_llm_budget_ignores_zero_budget() -> None:
    """Лимит 0 сравнивать не с чем — деления на ноль быть не должно."""
    monitor = make_monitor()

    assert monitor.evaluate(budget_usage(1.0, budget=0.0), now=0.0) == []


def test_read_llm_budget_usage_understands_real_exposition_format() -> None:
    """Парсер и выдача prometheus_client не должны разойтись молча (канон
    test_sum_server_errors_understands_real_exposition_format)."""
    from prometheus_client import generate_latest

    from hospitality.shared.metrics import set_llm_daily_budget

    set_llm_daily_budget({TENANT_A: (Decimal("8.7"), Decimal("10"))})
    usage = read_llm_budget_usage(generate_latest().decode())

    assert usage == [TenantBudgetUsage(tenant_id=TENANT_A, spent_usd=8.7, budget_usd=10.0)]

    set_llm_daily_budget({})
    assert read_llm_budget_usage(generate_latest().decode()) == []


def test_read_llm_budget_usage_needs_both_numbers() -> None:
    """Расход без лимита (или наоборот) — половина снимка; доля из неё
    не считается, тенант пропускается."""
    text = f'llm_daily_spend_usd{{tenant_id="{TENANT_A}"}} 8.7\n'

    assert read_llm_budget_usage(text) == []


def test_read_gauge_understands_real_exposition_format() -> None:
    """Парсер и выдача prometheus_client не должны разойтись молча (канон
    test_sum_server_errors_understands_real_exposition_format)."""
    from prometheus_client import generate_latest

    from hospitality.shared.metrics import worker_heartbeat_age_seconds

    worker_heartbeat_age_seconds.set(42.5)
    assert read_gauge(generate_latest().decode(), "worker_heartbeat_age_seconds") == 42.5

    worker_heartbeat_age_seconds.set(float("nan"))
    assert read_gauge(generate_latest().decode(), "worker_heartbeat_age_seconds") is None
    assert read_gauge(generate_latest().decode(), "no_such_metric") is None


# ---------------------------------------------------------------------------
# Свежесть бэкапа (issue #106)
# ---------------------------------------------------------------------------


def test_stale_backup_alerts_once_and_recovers() -> None:
    """Ночной бэкап перестал появляться — об этом обязан сказать watchdog."""
    monitor = make_monitor()

    fresh = monitor.evaluate(ready_probe(ok=True, backup=backup_probe(age_hours=3)), now=0.0)
    stale = monitor.evaluate(ready_probe(ok=True, backup=backup_probe(age_hours=41)), now=60.0)
    still_stale = monitor.evaluate(
        ready_probe(ok=True, backup=backup_probe(age_hours=42)), now=120.0
    )
    recovery = monitor.evaluate(ready_probe(ok=True, backup=backup_probe(age_hours=1)), now=180.0)

    assert fresh == []
    assert len(stale) == 1 and ERR_BACKUP_STALE in stale[0]
    assert "41 ч" in stale[0] and "порог 30 ч" in stale[0]
    assert still_stale == []  # алерт уже активен — не лента каждую минуту
    assert len(recovery) == 1 and "✅" in recovery[0] and ERR_BACKUP_STALE in recovery[0]


def test_backup_directory_without_files_alerts() -> None:
    """Каталог открылся, но ночных дампов в нём нет — это тоже «бэкапа нет»."""
    monitor = make_monitor()

    messages = monitor.evaluate(ready_probe(ok=True, backup=backup_probe()), now=0.0)

    assert len(messages) == 1 and ERR_BACKUP_STALE in messages[0]
    assert "нет ни одного" in messages[0]


def test_unreadable_backup_directory_alerts() -> None:
    """«Не знаю» про бэкап — это алерт, а не молчание (в отличие от метрик).

    У линий по /metrics молчание оправдано тем, что недоступное приложение уже
    покрыто ERR-OPS-001. Нечитаемый каталог бэкапов не покрыт ничем: это ровно
    та тишина, ради которой заведена issue #106.
    """
    monitor = make_monitor()

    messages = monitor.evaluate(
        ready_probe(ok=True, backup=backup_probe(readable=False, error="Permission denied")),
        now=0.0,
    )

    assert len(messages) == 1 and ERR_BACKUP_STALE in messages[0]
    assert "Permission denied" in messages[0]


def test_backup_line_is_silent_when_directory_not_configured() -> None:
    """ALERT_BACKUP_DIR пуст (dev, CI) — линия выключена и не шумит."""
    monitor = make_monitor()

    assert monitor.evaluate(ready_probe(ok=True, backup=None), now=0.0) == []
    assert monitor.evaluate(ready_probe(ok=True, backup=None), now=60.0) == []


def _write_dump(directory: Path, name: str, *, age_hours: float) -> Path:
    path = directory / name
    path.write_bytes(b"age-encryption.org/v1\n")
    stamp = time.time() - age_hours * _HOUR
    os.utime(path, (stamp, stamp))
    return path


def test_probe_backups_ignores_deploy_snapshots(tmp_path: Path) -> None:
    """Снимок перед миграцией за бэкап не считается (решение PR #284, #135).

    Иначе умерший ночной cron маскировался бы снимками деплоя: на staging их
    несколько в день, и каждый выглядел бы «свежим бэкапом» — ровно та тишина,
    против которой заведена issue #106.
    """
    _write_dump(tmp_path, "hospitality-20260817T030001Z.dump.age", age_hours=41)
    _write_dump(tmp_path, "pre-migrate-a73b8aafdad2-20260819T081500Z.dump.age", age_hours=1)

    probe = probe_backups(str(tmp_path), now=time.time())

    assert probe is not None and probe.readable
    assert probe.latest_age_seconds is not None
    assert 40.5 * _HOUR < probe.latest_age_seconds < 41.5 * _HOUR


def test_probe_backups_takes_the_newest_and_ignores_leftovers(tmp_path: Path) -> None:
    """Возраст считается по свежайшему дампу; чужие файлы каталога не в счёт."""
    _write_dump(tmp_path, "hospitality-20260817T030001Z.dump.age", age_hours=48)
    _write_dump(tmp_path, "hospitality-20260819T030001Z.dump.age", age_hours=5)
    # Обрывок прерванного прогона и лог cron — не бэкапы.
    _write_dump(tmp_path, "hospitality-20260819T090001Z.dump.age.part", age_hours=0.1)
    _write_dump(tmp_path, "backup.log", age_hours=0.1)

    probe = probe_backups(str(tmp_path), now=time.time())

    assert probe is not None and probe.latest_age_seconds is not None
    assert 4.5 * _HOUR < probe.latest_age_seconds < 5.5 * _HOUR


def test_probe_backups_reports_empty_and_missing_directory(tmp_path: Path) -> None:
    empty = probe_backups(str(tmp_path), now=time.time())
    missing = probe_backups(str(tmp_path / "нет-такого"), now=time.time())

    assert empty is not None and empty.readable and empty.latest_age_seconds is None
    assert missing is not None and not missing.readable and missing.error


def test_probe_backups_reports_unreadable_directory(tmp_path: Path) -> None:
    """Каталог есть, но прав на него нет — watchdog говорит об этом, а не падает.

    Это не гипотеза: каталог бэкапов принадлежит пользователю deploy на хосте,
    а алертер читает его из контейнера под другим uid (10001).
    """
    closed = tmp_path / "closed"
    closed.mkdir()
    closed.chmod(0o000)
    try:
        try:
            os.listdir(closed)
        except OSError:
            pass
        else:  # root игнорирует права — проверять нечего
            pytest.skip("тест запущен от root: права каталога не ограничивают")

        probe = probe_backups(str(closed), now=time.time())

        assert probe is not None and not probe.readable and probe.error
    finally:
        closed.chmod(0o700)


def test_probe_backups_disabled_by_empty_directory() -> None:
    assert probe_backups("", now=time.time()) is None


def test_probe_backups_clamps_backup_from_the_future(tmp_path: Path) -> None:
    """Часы сервера ушли назад — это ноль, а не отрицательный возраст."""
    _write_dump(tmp_path, "hospitality-20260819T030001Z.dump.age", age_hours=-2)

    probe = probe_backups(str(tmp_path), now=time.time())

    assert probe is not None and probe.latest_age_seconds == 0.0


# ---------------------------------------------------------------------------
# Цикл run_alerter (httpx.MockTransport, без сети и без сна)
# ---------------------------------------------------------------------------


@pytest.fixture
def alerter_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TELEGRAM_ALERT_BOT_TOKEN", "alert-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "-100777")
    monkeypatch.setenv("ALERT_POLL_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("ALERT_READY_FAILURE_THRESHOLD", "2")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeStagingStack:
    """Фейковые /health/ready, /metrics и Telegram Bot API в одном транспорте."""

    def __init__(self, ready_statuses: list[int], metrics_text: str = SAMPLE_METRICS) -> None:
        self.ready_statuses = ready_statuses
        self.metrics_text = metrics_text
        self.sent_messages: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health/ready":
            status = self.ready_statuses.pop(0)
            return httpx.Response(status, json={"status": "ok" if status == 200 else "unavailable"})
        if request.url.path == "/metrics":
            return httpx.Response(200, text=self.metrics_text)
        if request.url.path.endswith("/sendMessage"):
            self.sent_messages.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"неожиданный запрос алертера: {request.url}")


def test_run_alerter_sends_alert_and_recovery(alerter_settings: None) -> None:
    stack = FakeStagingStack(ready_statuses=[503, 503, 200])

    run_alerter(iterations=3, transport=httpx.MockTransport(stack.handler))

    assert len(stack.sent_messages) == 2
    alert, recovery = stack.sent_messages
    assert alert["chat_id"] == "-100777"
    assert ERR_READY_UNAVAILABLE in alert["text"] and "🔴" in alert["text"]
    assert "✅" in recovery["text"]


def test_run_alerter_reports_stalled_worker_from_metrics(alerter_settings: None) -> None:
    """Проводка обеих метрик #136 из /metrics в ProbeResult — на машине.

    Тесты машины состояний собирают ProbeResult руками и до probe_application не
    доходят; SAMPLE_METRICS держит оба значения ниже порогов, поэтому от проводки
    в нём ничего не зависит. Этот тест — единственное место, где ломается весь
    тракт «HTTP-ответ → парсер → машина → отправка», если строку проводки в
    probe_application убрать или ошибиться в имени метрики: приложение живо
    (/health/ready = 200), а оба алерта обязаны прийти.
    """
    stack = FakeStagingStack(ready_statuses=[200], metrics_text=STALLED_WORKER_METRICS)

    run_alerter(iterations=1, transport=httpx.MockTransport(stack.handler))

    texts = [message["text"] for message in stack.sent_messages]
    assert len(texts) == 2, texts
    assert any(ERR_WORKER_STALLED in text and "15 мин" in text for text in texts)
    assert any(ERR_OUTBOX_BACKLOG in text and "500" in text for text in texts)


def test_run_alerter_reports_llm_budget_from_metrics(alerter_settings: None) -> None:
    """Тракт «HTTP-ответ → парсер → машина → отправка» для issue #103.

    Тесты машины собирают ProbeResult руками и до probe_application не доходят,
    а SAMPLE_METRICS пар бюджета не содержит вовсе — поэтому имя метрики и
    строка проводки проверяются здесь: приложение живо, отель на 87% лимита,
    сосед на 3%, алерт обязан прийти ровно один и назвать первого.
    """
    stack = FakeStagingStack(ready_statuses=[200], metrics_text=NEAR_BUDGET_METRICS)

    run_alerter(iterations=1, transport=httpx.MockTransport(stack.handler))

    texts = [message["text"] for message in stack.sent_messages]
    assert len(texts) == 1, texts
    assert ERR_LLM_BUDGET_NEAR_LIMIT in texts[0] and "87%" in texts[0]
    assert f"tenant: {TENANT_A}" in texts[0] and TENANT_B not in texts[0]


def test_run_alerter_reports_stale_backup_from_disk(
    alerter_settings: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Тракт «каталог на диске → машина → отправка» для issue #106.

    Тесты машины состояний собирают ProbeResult руками и до probe_backups не
    доходят; это единственное место, где покраснеет снятая проводка каталога в
    run_alerter: приложение живо, метрики в норме, а алерт обязан прийти из-за
    единственного слишком старого дампа на диске.
    """
    _write_dump(tmp_path, "hospitality-20260817T030001Z.dump.age", age_hours=41)
    monkeypatch.setenv("ALERT_BACKUP_DIR", str(tmp_path))
    get_settings.cache_clear()
    stack = FakeStagingStack(ready_statuses=[200])

    run_alerter(iterations=1, transport=httpx.MockTransport(stack.handler))

    texts = [message["text"] for message in stack.sent_messages]
    assert len(texts) == 1, texts
    assert ERR_BACKUP_STALE in texts[0] and "41 ч" in texts[0]


def test_run_alerter_survives_telegram_send_failure(alerter_settings: None) -> None:
    """Сбой отправки логируется, но не роняет цикл (следующая итерация живёт)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health/ready":
            return httpx.Response(503, json={"status": "unavailable"})
        if request.url.path == "/metrics":
            return httpx.Response(200, text=SAMPLE_METRICS)
        return httpx.Response(500, json={"ok": False})

    run_alerter(iterations=3, transport=httpx.MockTransport(handler))  # не бросает


def test_half_configured_pair_fails_fast(
    alerter_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "")
    get_settings.cache_clear()

    with pytest.raises(SystemExit):
        run_alerter(iterations=1)


def test_unconfigured_alerter_is_passive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_ALERT_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "")
    get_settings.cache_clear()

    def forbid_sleep(_seconds: float) -> None:
        raise AssertionError("одна итерация пассивного цикла не должна спать")

    monkeypatch.setattr("hospitality.tools.alerter.time.sleep", forbid_sleep)
    try:
        run_alerter(iterations=1)  # не бросает и не ходит в сеть
    finally:
        get_settings.cache_clear()
