"""Тракт операционных алертов: текст и доставка в Telegram-канал команды (§10 п.8).

Один канон на «сказать человеку, что сломалось» — им пользуются два процесса:
watchdog `hospitality.tools.alerter` (внешний опрос `/health/ready` и
`/metrics`, ERR-OPS-001/002) и воркер, докладывающий о событиях, ушедших в
dead-letter (ERR-EVENTS-002, issue #133). До issue #133 канон жил внутри
алертера — второй отправитель скопировал бы формат и разошёлся с ним.

Отправка — прямой ``sendMessage`` Telegram Bot API, а не порт с адаптером
(ADR-007) и не `channels/telegram`: тракт алертов обязан работать именно тогда,
когда сломан код каналов или адаптеров, и не имеет права отличаться в dev и
prod (Fake-адаптер сделал бы алерт «доставленным» в никуда). Прецедент того же
рода в ядре — `shared/sentry.py`.

Конфигурация — пара ``TELEGRAM_ALERT_BOT_TOKEN`` + ``TELEGRAM_ALERT_CHAT_ID``:
обе пустые = алертинг выключен (dev, CI), заполнена ровно одна — ошибка
конфигурации и немедленное падение процесса (fail-fast, §11).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx

from hospitality.shared.config import Settings

# Отправитель алерта: текст → доставка. Инъекция, а не глобальная функция —
# периодические задачи воркера получают его от composition root (канон
# `remind_unclaimed_requests`), а тесты подставляют свой без сети.
AlertSender = Callable[[str], Awaitable[None]]

_HTTP_TIMEOUT_SECONDS = 5.0

# Тенант и correlation id платформенного алерта, у которого их нет: внешний
# опрос не привязан ни к тенанту, ни к запросу (след ищется по времени и коду).
PLATFORM_TENANT = "platform"
NO_CORRELATION_ID = "—"


def format_alert(
    *,
    error_code: str,
    title: str,
    detail: str,
    environment: str,
    runbook_url: str,
    emoji: str = "🔴",
    tenant: str = PLATFORM_TENANT,
    correlation_id: str = NO_CORRELATION_ID,
) -> str:
    """Текст алерта по §10 п.8: код, тенант, correlation id, детали, runbook.

    Якорь статьи runbook собирается из кода ошибки (``ERR-OPS-001`` →
    ``#err-ops-001``): статья с таким заголовком обязана существовать —
    алерт без диагноза бесполезен.
    """
    anchor = error_code.lower()
    return (
        f"{emoji} [{environment}] {error_code}: {title}\n"
        f"детали: {detail}\n"
        f"tenant: {tenant} · correlation_id: {correlation_id}\n"
        f"runbook: {runbook_url}#{anchor}"
    )


def alerting_configured(settings: Settings) -> bool:
    """Настроен ли тракт алертов; полузаполненная пара — падение (fail-fast, §11)."""
    configured = [bool(settings.telegram_alert_bot_token), bool(settings.telegram_alert_chat_id)]
    if any(configured) and not all(configured):
        raise SystemExit(
            "TELEGRAM_ALERT_BOT_TOKEN и TELEGRAM_ALERT_CHAT_ID задаются только парой "
            "(docs/runbooks/alerts.md); заполнен ровно один — исправьте .env"
        )
    return all(configured)


def alert_request(settings: Settings, text: str) -> tuple[str, dict[str, str]]:
    """URL и тело ``sendMessage`` — общая часть синхронного и асинхронного отправителей."""
    url = f"{settings.telegram_api_base_url}/bot{settings.telegram_alert_bot_token}/sendMessage"
    return url, {"chat_id": settings.telegram_alert_chat_id, "text": text}


async def send_alert(settings: Settings, text: str) -> None:
    """Отправить алерт в канал команды; сбой отправки ПРОБРАСЫВАЕТСЯ.

    Асимметрия с алертером намеренная: у watchdog'а состояние в памяти и он
    просто попробует на следующем опросе, а вызывающий здесь помечает в БД
    «человеку сказано» — пометить это после провалившейся отправки значило бы
    похоронить событие молча, ровно то, против чего issue #133.

    Клиент создаётся на вызов: алерты редки, а лишний долгоживущий ресурс в
    цикле воркера — лишняя связность.
    """
    url, payload = alert_request(settings, text)
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
