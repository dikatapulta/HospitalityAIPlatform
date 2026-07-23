"""Стенд smoke-набора (Task 0019, §13.6): настройки, HTTP-клиент, русский вывод.

Smoke — чёрный ящик: НИКАКИХ импортов из пакета `hospitality` (spec 0019).
Набор видит систему как внешний мир — по HTTP, поэтому без изменений гоняется
против локальной среды (`make smoke`), CI-окружения и staging
(`make smoke-staging`).

Вывод — инструмент приёмки основателя: по строке на сценарий (первая строка
docstring теста), ✅/❌ и краткая причина падения без стектрейса.
"""

from __future__ import annotations

import time
from collections.abc import Generator, Iterator
from typing import TYPE_CHECKING

import httpx
import pytest
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from _pytest.terminal import TerminalReporter

# Ходы гостя ждут настоящую модель: до LLM_MAX_ATTEMPTS попыток по
# LLM_TIMEOUT_SECONDS внутри приложения — клиент обязан ждать дольше.
HTTP_TIMEOUT_SECONDS = 120.0

# Сколько ждать готовности среды: свежеподнятый `make dev`/CI-стек несколько
# секунд собирает приложение, это не повод падать.
READY_TIMEOUT_SECONDS = 30.0


class SmokeSettings(BaseSettings):
    """Куда и с какими секретами идёт smoke (spec 0019, таблица конфигурации).

    Секреты берутся из окружения (`SMOKE_*` — так их передаёт
    ops/smoke-staging.sh) с fallback на локальный `.env` — тот же файл, из
    которого их читает приложение `make dev`, поэтому локальный запуск не
    требует настройки вовсе.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    base_url: str = Field(
        default="http://localhost:8000", validation_alias=AliasChoices("SMOKE_BASE_URL")
    )
    webhook_secret: str = Field(
        default="",
        validation_alias=AliasChoices("SMOKE_WEBHOOK_SECRET", "TELEGRAM_WEBHOOK_SECRET"),
    )
    service_token: str = Field(
        default="dev-service-token",
        validation_alias=AliasChoices("SMOKE_SERVICE_TOKEN", "SERVICE_TOKEN"),
    )
    anthropic_api_key: str = Field(default="", validation_alias=AliasChoices("ANTHROPIC_API_KEY"))


@pytest.fixture(scope="session")
def settings() -> SmokeSettings:
    return SmokeSettings()


@pytest.fixture(scope="session")
def client(settings: SmokeSettings) -> Iterator[httpx.Client]:
    """HTTP-клиент к проверяемой среде + preflight с диагностикой по-русски."""
    with httpx.Client(base_url=settings.base_url, timeout=HTTP_TIMEOUT_SECONDS) as client:
        _preflight(client, settings)
        yield client


def _preflight(client: httpx.Client, settings: SmokeSettings) -> None:
    """Проверить, что среда вообще пригодна для сценариев, до их запуска.

    Каждое сообщение — действие для основателя, а не диагноз для программиста.
    """
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while True:
        try:
            ready = client.get("/health/ready")
            if ready.status_code == 200:
                break
            problem = f"/health/ready ответил {ready.status_code} {ready.text!r}"
        except httpx.HTTPError as error:
            problem = f"среда не отвечает ({error!r})"
        if time.monotonic() >= deadline:
            pytest.fail(
                f"Среда {settings.base_url} нездорова: {problem}. Локально: подними её "
                "командами make dev, make migrate, make seed; если поднята — смотри "
                "docker compose logs (docs/runbooks/alerts.md, ERR-OPS-001).",
                pytrace=False,
            )
        time.sleep(1.0)
    if not settings.webhook_secret:
        pytest.fail(
            "Не задан секрет вебхука Telegram. Локально: добавь в .env строку "
            "TELEGRAM_WEBHOOK_SECRET=dev-webhook-secret (см. .env.example) и перезапусти "
            "make dev. Для staging секрет подтягивает ops/smoke-staging.sh.",
            pytrace=False,
        )
    is_local = "localhost" in settings.base_url or "127.0.0.1" in settings.base_url
    if is_local and not settings.anthropic_api_key:
        pytest.fail(
            "Smoke гоняет настоящую модель, а ANTHROPIC_API_KEY в .env пуст. "
            "Добавь ключ в .env и перезапусти make dev.",
            pytrace=False,
        )


@pytest.fixture(scope="session")
def run_id() -> int:
    """Уникальный номер прогона: из него делаются chat_id и update_id, чтобы
    прогоны не пересекались ни идемпотентностью (P-8), ни историей диалога."""
    return time.time_ns() // 1_000_000  # миллисекунды: влезает в int64 Telegram


# ---------------------------------------------------------------------------
# Русский вывод для основателя: строка на сценарий вместо точек pytest.
# Хуки вывода вызываются на весь прогон (conftest грузится и в `make check`),
# поэтому каждый защищён признаком smoke_title — метку несут только тесты
# из tests/smoke (runtest-хуки conftest применяются только к своему каталогу).
# Строки печатаются напрямую в terminalreporter: штатный вывод статусов виден
# только с -v и с мусорным префиксом nodeid, а smoke должен читаться в тихом
# режиме make smoke (--tb=no -rN).
# ---------------------------------------------------------------------------

_STATUS_MARKS = {"passed": "✅", "failed": "❌", "error": "💥"}

_config: pytest.Config | None = None


def pytest_configure(config: pytest.Config) -> None:
    global _config
    _config = config


def _scenario_title(item: pytest.Item) -> str:
    """Первая строка docstring теста — формулировка сценария на языке бизнеса."""
    function = getattr(item, "function", None)
    docstring: str = (function.__doc__ if function is not None else None) or ""
    stripped = docstring.strip()
    first_line = stripped.splitlines()[0] if stripped else item.name
    return first_line.rstrip(".")


def _failure_reason(report: pytest.TestReport) -> str:
    """Короткая причина падения: сообщение assert/fail без стектрейса."""
    crash = getattr(report.longrepr, "reprcrash", None)
    message = str(getattr(crash, "message", None) or report.longrepr or "")
    for prefix in ("Failed: ", "AssertionError: "):
        message = message.removeprefix(prefix)
    return message.split("\n")[0]


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    report.smoke_title = _scenario_title(item)  # type: ignore[attr-defined]
    return report


def pytest_report_teststatus(
    report: pytest.CollectReport | pytest.TestReport, config: pytest.Config
) -> tuple[str, str, str] | None:
    """Погасить штатные точки/буквы прогресса у smoke-тестов (строку сценария
    печатает pytest_runtest_logreport)."""
    if not isinstance(report, pytest.TestReport):
        return None
    if getattr(report, "smoke_title", None) is None:
        return None  # не smoke-тест: набор попал в общий прогон — не вмешиваемся
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        return report.outcome, "", ""
    return "", "", ""  # setup/teardown не считаются отдельными исходами


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Напечатать строку сценария по-русски сразу по завершении теста."""
    title = getattr(report, "smoke_title", None)
    if title is None or _config is None:
        return
    terminal = _config.pluginmanager.getplugin("terminalreporter")
    if terminal is None:
        return
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        if report.skipped:
            reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else ""
            reason = str(reason).removeprefix("Skipped: ")
            terminal.write_line(f"⏭️ {title} — пропущен: {reason}")
            return
        mark = _STATUS_MARKS.get(report.outcome, report.outcome)
        if report.failed:
            terminal.write_line(f"{mark} {title} — {_failure_reason(report)}")
        else:
            terminal.write_line(f"{mark} {title} ({report.duration:.1f} с)")


def pytest_terminal_summary(
    terminalreporter: TerminalReporter, exitstatus: int, config: pytest.Config
) -> None:
    """Итоговая строка: понятна без чтения кода (DoD Task 0019)."""
    reports = [
        report
        for key in ("passed", "failed", "error")
        for report in terminalreporter.stats.get(key, [])
        if getattr(report, "smoke_title", None) is not None
    ]
    if not reports:  # обычный юнит-прогон — smoke-итог неуместен
        return
    failed = sum(1 for report in reports if report.outcome in ("failed", "error"))
    terminalreporter.write_line("")
    if failed:
        terminalreporter.write_line(
            f"Итог: провалено сценариев — {failed}. Система требует внимания."
        )
    else:
        terminalreporter.write_line("Итог: система жива, сценарии гостя работают.")
