"""Демо-картина тестового отеля на staging к показу руководству (#605).

Наполняет ТОЛЬКО тенант `demo-hotel`: персонал ссылками-приглашениями
(`tools/demo_showcase_staff.py`), проживания, выезды на всех шагах доски и заявки
(`tools/demo_showcase_board.py`). Модели нет, платных вызовов нет. Идемпотентна:
повторный запуск в тот же день отеля приводит к той же картине; `--reset` закрывает
прежнюю картину и строит её заново. Чужие строки не трогает.

Где можно: только staging. Отказ, если не выполнено хоть одно — флаг
`--i-know-this-is-staging`, `SENTRY_ENVIRONMENT=staging`, отель контура
(`TELEGRAM_TENANT_SLUG`) — `demo-hotel`, адрес контура (`PUBLIC_BASE_URL`) —
`staging.…`. У гостевой копии отеля `SENTRY_ENVIRONMENT` тоже `staging` (#389),
поэтому её отсекают отель и адрес контура.

Срок «просрочена»: задним числом заявку не создать, она стареет сама. Команда
ставит демо-отелю срок напоминания двум службам (если своего у них нет): заявка
«Техническая неисправность» просрочена через 10 минут, «Уборка номера» — через 3 часа.

Запуск — шаг основателя, рунбук: docs/runbooks/deploy.md, «Демо-картина к показу».

    docker compose -f docker-compose.staging.yml --env-file .env exec app \\
        python -m hospitality.tools.demo_showcase --i-know-this-is-staging [--reset]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import UTC, datetime, time
from typing import Final
from urllib.parse import urlsplit

from sqlalchemy import select

from hospitality.platform.config import TenantConfig, mutate_tenant_config
from hospitality.platform.models import Tenant
from hospitality.platform.seed import DEMO_TENANT_SLUG
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import configure_logging, get_logger
from hospitality.shared.tenancy import tenant_context
from hospitality.tools.demo_showcase_board import (
    OVERDUE_SUMMARY,
    ensure_requests,
    ensure_stays,
    ensure_turnovers,
)
from hospitality.tools.demo_showcase_staff import (
    DemoShowcaseError,
    assign_floors,
    ensure_reception_category,
    ensure_staff,
)

logger = get_logger(module=__name__)

STAGING_ENVIRONMENT: Final = "staging"
CONFIRM_FLAG: Final = "--i-know-this-is-staging"

# Срок напоминания по службам (минуты): «просрочена» и «новая» на одном показе.
DEMO_REMINDER_MINUTES: Final = {"maintenance": 10, "housekeeping": 180}

# «Новая + просроченная» держится от 10 минут до 3 часов после запуска (сроки выше).
SHOW_DAY_HINT: Final = (
    "В день показа: запустите с --reset за 15–60 минут до начала — "
    "тогда на экране будут и новая, и просроченная заявка."
)


def staging_refusals(*, confirmed: bool) -> list[str]:
    """Почему здесь запускать нельзя; пустой список — это staging."""
    settings = get_settings()
    refusals: list[str] = []
    if not confirmed:
        refusals.append(f"нет флага {CONFIRM_FLAG}")
    if settings.sentry_environment != STAGING_ENVIRONMENT:
        refusals.append(
            f"SENTRY_ENVIRONMENT={settings.sentry_environment!r}, а нужен {STAGING_ENVIRONMENT!r}"
        )
    if settings.telegram_tenant_slug != DEMO_TENANT_SLUG:
        # Значение не печатается: код отеля пилота в репозиторий и логи не попадает.
        refusals.append(
            f"отель этого контура (TELEGRAM_TENANT_SLUG) — не {DEMO_TENANT_SLUG}: "
            "похоже на копию отеля, туда демо нельзя"
        )
    host = urlsplit(settings.public_base_url).hostname or ""
    if not host.startswith(f"{STAGING_ENVIRONMENT}."):
        refusals.append(f"адрес контура (PUBLIC_BASE_URL) {host!r} — не staging.…")
    return refusals


def _with_demo_reminders(config: TenantConfig) -> TenantConfig:
    current = config.request_reminder_minutes_by_category
    missing = {key: minutes for key, minutes in DEMO_REMINDER_MINUTES.items() if key not in current}
    if not missing:
        return config
    return config.model_copy(
        update={"request_reminder_minutes_by_category": {**current, **missing}}
    )


async def _demo_tenant_id() -> uuid.UUID:
    async with platform_session_scope() as session:
        tenant_id: uuid.UUID | None = await session.scalar(
            select(Tenant.id).where(Tenant.slug == DEMO_TENANT_SLUG)
        )
    if tenant_id is None:
        raise DemoShowcaseError(
            f"Тенанта {DEMO_TENANT_SLUG} нет — сначала python -m hospitality.tools.seed."
        )
    return tenant_id


async def run_showcase(
    *, confirmed: bool, reset: bool = False, now: datetime | None = None
) -> list[str]:
    """Построить картину демо-отеля; вернуть строки для печати.

    `now` — момент «сейчас» для границы дня отеля и сроков выезда (тесты сдвигают
    его на завтра, чтобы проверить смену дня); None — настоящее время.
    """
    refusals = staging_refusals(confirmed=confirmed)
    if refusals:
        raise DemoShowcaseError("Это не staging — ничего не сделано: " + "; ".join(refusals) + ".")
    moment = now or utc_now()
    tenant_id = await _demo_tenant_id()
    async with platform_session_scope() as session:
        config = await mutate_tenant_config(session, tenant_id, _with_demo_reminders)
    zone = config.tzinfo
    midnight = datetime.combine(moment.astimezone(zone).date(), time(0), tzinfo=zone).astimezone(
        UTC
    )
    with tenant_context(tenant_id):
        reception_added = await ensure_reception_category()
        actors, staff_lines = await ensure_staff(tenant_id)
        floors_line = await assign_floors(actors)
        stays_line = await ensure_stays(actors, zone=zone, now=moment, reset=reset)
        turnovers_line = await ensure_turnovers(actors, midnight=midnight, reset=reset)
        requests_line = await ensure_requests(actors, midnight=midnight, reset=reset)
    overdue_delay = config.reminder_delay_for("maintenance")
    logger.info("demo_showcase_built", tenant_id=str(tenant_id), reset=reset)
    lines = [f"Демо-картина отеля {DEMO_TENANT_SLUG} готова.", "Персонал:", *staff_lines]
    if reception_added:
        lines.append("  Служба «Ресепшен» заведена — без неё ресепшен не пригласить.")
    lines += [floors_line, stays_line, turnovers_line, requests_line]
    if overdue_delay is not None:
        overdue_at = (moment + overdue_delay).astimezone(zone).strftime("%H:%M")
        lines.append(f"Заявка «{OVERDUE_SUMMARY}» станет просроченной около {overdue_at}.")
    if not get_settings().enable_turnover_board:
        lines.append("Внимание: доска выездов выключена (ENABLE_TURNOVER_BOARD) — её не видно.")
    lines.append(SHOW_DAY_HINT)
    return lines


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hospitality.tools.demo_showcase",
        description=f"Демо-картина тенанта {DEMO_TENANT_SLUG} на staging к показу (#605).",
    )
    parser.add_argument(
        CONFIRM_FLAG,
        dest="confirmed",
        action="store_true",
        help="Подтверждение, что это staging (без него — отказ).",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Закрыть прежнюю демо-картину и построить заново (свежие времена).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI; возвращает код возврата процесса."""
    configure_logging(get_settings().log_level)
    args = _build_parser().parse_args(argv)
    try:
        lines = asyncio.run(run_showcase(confirmed=args.confirmed, reset=args.reset))
    except DemoShowcaseError as error:
        print(str(error), file=sys.stderr)
        return 1
    except AppError as error:
        print(f"{error.message} ({error.code}, docs/runbooks/errors.md)", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа; логика покрыта тестами
    raise SystemExit(main())
