"""Красный CI на ``main`` — сообщение в канал команды (issue #413, ERR-OPS-013).

Уведомления GitHub Actions по умолчанию выключены, и красный ``main`` висел ≈50 ч
(27–29.09), пока кто-нибудь случайно не открыл вкладку Actions. Модуль зовёт
последняя job workflow — ``alert-red-main`` в ``ci.yml`` и ``smoke.yml``; когда
звать (провал на ``main``), решает условие job, здесь — только что сказать.

Текст и доставка — канон ``shared/alerting``, тот же, что у алертера и воркера.
Вход — окружение job: пара ``TELEGRAM_ALERT_*`` из секретов репозитория,
``CI_NEEDS_JSON`` (``toJSON(needs)`` — итог каждой job) и ``CI_COMMIT_TITLE``;
коммит, workflow и адрес прогона — встроенные ``GITHUB_*`` раннера. Пустая пара
секретов — не ошибка: notice и код 0, как guard ``deploy-staging``.

Зелёного «✅» нет: состояние между прогонами хранить негде, а следующий зелёный
прогон и так виден в Actions.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping

import httpx
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from hospitality.shared.alerting import alerting_configured, format_alert, send_alert
from hospitality.shared.config import Settings

# Код каталога (docs/runbooks/errors.md, R-8).
ERR_CI_RED_MAIN = "ERR-OPS-013"

ALERT_TITLE = "проверки на main красные"

# Окружение в префиксе алерта: «[ci]», а не умолчание Settings «[dev]».
_ENVIRONMENT = "ci"

# Деплой, которого не было: его пропуск — главное для решения «обновлять ли копию».
_DEPLOY_JOB = "deploy-staging"

# Заголовок коммита длиннее — обрезается: алерт читают с телефона.
_COMMIT_TITLE_LIMIT = 100


class _JobOutcome(BaseModel):
    """Итог одной job из ``toJSON(needs)``; ``outputs`` не нужны."""

    model_config = ConfigDict(extra="ignore")

    result: str


_NEEDS = TypeAdapter(dict[str, _JobOutcome])


def describe_failure(
    *, workflow: str, needs_json: str, sha: str, commit_title: str, run_url: str
) -> str:
    """Строка «детали» алерта: что упало, что из-за этого не запускалось, где смотреть."""
    parts = [f"workflow «{workflow}»"]
    try:
        needs = _NEEDS.validate_json(needs_json)
    except ValidationError:
        # Алерт важнее подробностей: без списка job он всё равно уходит.
        parts.append("какие job упали — в прогоне")
    else:
        failed = [job for job, outcome in needs.items() if outcome.result == "failure"]
        not_run = [
            job for job, outcome in needs.items() if outcome.result in ("skipped", "cancelled")
        ]
        if failed:
            parts.append("упали: " + ", ".join(failed))
        if not_run:
            skipped = "не запускались: " + ", ".join(not_run)
            if _DEPLOY_JOB in not_run:
                skipped += " — staging не обновлён"
            parts.append(skipped)

    commit = f"коммит {sha[:7]}"
    title = commit_title.strip().splitlines()[0].strip() if commit_title.strip() else ""
    if title:
        if len(title) > _COMMIT_TITLE_LIMIT:
            title = title[: _COMMIT_TITLE_LIMIT - 1].rstrip() + "…"
        commit += f" «{title}»"
    return f"{'; '.join(parts)} · {commit} · прогон: {run_url}"


def build_alert_text(settings: Settings, environ: Mapping[str, str]) -> str:
    """Полный текст алерта из окружения job GitHub Actions."""
    run_url = (
        f"{environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
        f"{environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{environ.get('GITHUB_RUN_ID', '')}"
    )
    detail = describe_failure(
        workflow=environ.get("GITHUB_WORKFLOW", "?"),
        needs_json=environ.get("CI_NEEDS_JSON", ""),
        sha=environ.get("GITHUB_SHA", "?"),
        commit_title=environ.get("CI_COMMIT_TITLE", ""),
        run_url=run_url,
    )
    return format_alert(
        error_code=ERR_CI_RED_MAIN,
        title=ALERT_TITLE,
        detail=detail,
        environment=_ENVIRONMENT,
        runbook_url=settings.alert_runbook_url,
    )


def main(
    settings: Settings | None = None,
    environ: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Отправить алерт; пустая пара секретов — notice и 0, сбой отправки — исключение."""
    settings = settings or Settings()
    if not alerting_configured(settings):
        print(
            "::notice::Секреты TELEGRAM_ALERT_BOT_TOKEN и TELEGRAM_ALERT_CHAT_ID не заданы — "
            f"алерт {ERR_CI_RED_MAIN} не отправлен (docs/runbooks/alerts.md#err-ops-013)."
        )
        return 0
    text = build_alert_text(settings, os.environ if environ is None else environ)
    asyncio.run(send_alert(settings, text, transport=transport))
    print(f"Алерт {ERR_CI_RED_MAIN} отправлен в канал команды.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
