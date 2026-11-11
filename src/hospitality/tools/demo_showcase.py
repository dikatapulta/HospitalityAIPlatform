"""Демо-картина тестового отеля на staging к показу руководству (#605).

Наполняет ТОЛЬКО тенант `demo-hotel`: персонал ссылками-приглашениями
(`tools/demo_showcase_staff.py`), проживания, выезды на всех шагах доски и заявки
(`tools/demo_showcase_board.py`), брони столика (`tools/demo_showcase_bookings.py`).
Модели нет, платных вызовов нет. Идемпотентна:
повторный запуск в тот же день отеля приводит к той же картине; `--reset` закрывает
прежнюю картину и строит её заново. Чужие строки не трогает.

Где можно: только staging. Отказ, если не выполнено хоть одно — флаг
`--i-know-this-is-staging`, `SENTRY_ENVIRONMENT=staging`, отель контура
(`TELEGRAM_TENANT_SLUG`) — `demo-hotel`, адрес контура (`PUBLIC_BASE_URL`) —
`staging.…`. У гостевой копии отеля `SENTRY_ENVIRONMENT` тоже `staging` (#389),
поэтому её отсекают отель и адрес контура. Признаки — `shared/staging_features.py`
(`staging_contour_refusals`), общие с выключателем `STAGING_ALL_FEATURES` (#1053).

Срок «просрочена»: задним числом заявку не создать, она стареет сама. Команда
ставит демо-отелю срок напоминания трём службам (если своего у них нет): заявка
«Техническая неисправность» просрочена через 10 минут, «Хаускипинг» и запрос на
столик в службе ресторана — через 3 часа.

Служба эскалаций (#785): без неё «позовите человека» и «сменить номер» приходят
только в Telegram, а карточки в кабинете нет (spec 0039 §2.3). Команда задаёт
демо-отелю «Ресепшен», если службы эскалаций у него нет; заданную не трогает.

Запуск — шаг основателя, рунбук: docs/runbooks/deploy.md, «Демо-картина к показу».
Ту же картину строит кнопка кабинета «Тестовые данные» (#740,
`staff_portal/demo_data.py`) — `build_showcase` без выпуска приглашений.

    docker compose -f docker-compose.staging.yml --env-file .env exec app \\
        python -m hospitality.tools.demo_showcase --i-know-this-is-staging [--reset]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Final

from sqlalchemy import select, text

from hospitality.platform.config import TenantConfig, mutate_tenant_config
from hospitality.platform.models import Tenant
from hospitality.platform.seed import DEMO_TENANT_SLUG
from hospitality.platform.staff_services import RECEPTION_SERVICE_KEY
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import configure_logging, get_logger
from hospitality.shared.staging_features import staging_contour_refusals
from hospitality.shared.tenancy import tenant_context
from hospitality.tools.demo_showcase_board import (
    DEMO_REQUESTS,
    DEMO_STAYS,
    OVERDUE_SUMMARY,
    RequestStatus,
    ensure_requests,
    ensure_stays,
    ensure_turnovers,
)
from hospitality.tools.demo_showcase_bookings import ensure_bookings
from hospitality.tools.demo_showcase_staff import (
    DemoShowcaseError,
    assign_floors,
    ensure_reception_category,
    ensure_staff,
)

logger = get_logger(module=__name__)

CONFIRM_FLAG: Final = "--i-know-this-is-staging"

# Ключ advisory-блокировки построения: других advisory-блокировок в проекте нет.
_BUILD_LOCK_KEY: Final = 740_605

# Срок напоминания по службам (минуты): «просрочена» и «новая» на одном показе;
# запрос на столик (служба ресторана) ждёт ответа весь показ, как уборка.
DEMO_REMINDER_MINUTES: Final = {"maintenance": 10, "housekeeping": 180, "fnb": 180}

# «Новая + просроченная» держится от 10 минут до 3 часов после запуска (сроки выше).
ESCALATION_LINE: Final = (
    "Служба эскалаций — ресепшен: «позовите человека» приходит карточкой в кабинет."
)

SHOW_DAY_HINT: Final = (
    "В день показа: запустите с --reset за 15–60 минут до начала — "
    "тогда на экране будут и новая, и просроченная заявка."
)


def staging_refusals(*, confirmed: bool) -> list[str]:
    """Почему здесь запускать нельзя; пустой список — это staging.

    Признаки контура — общие с выключателем `STAGING_ALL_FEATURES` (#1053).
    """
    refusals = [] if confirmed else [f"нет флага {CONFIRM_FLAG}"]
    return [*refusals, *staging_contour_refusals(get_settings())]


def _with_demo_settings(config: TenantConfig) -> TenantConfig:
    """Сроки напоминания и служба эскалаций демо-отеля — только недостающее."""
    current = config.request_reminder_minutes_by_category
    missing = {key: minutes for key, minutes in DEMO_REMINDER_MINUTES.items() if key not in current}
    update: dict[str, object] = {}
    if missing:
        update["request_reminder_minutes_by_category"] = {**current, **missing}
    if config.escalation_category_key is None:
        update["escalation_category_key"] = RECEPTION_SERVICE_KEY
    return config.model_copy(update=update) if update else config


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


@dataclass(frozen=True)
class ShowcaseReport:
    """Итог построения: строки для терминала и числа для кнопки кабинета (#740)."""

    lines: list[str]
    stays: int
    requests: int
    bookings: int  # 0 — бронь столика у демо-отеля выключена
    # Через сколько после построения станут просроченными новые заявки картины
    # (у служб без срока напоминания просрочки нет) — от раннего к позднему.
    overdue_after: list[timedelta]


async def run_showcase(
    *, confirmed: bool, reset: bool = False, now: datetime | None = None
) -> list[str]:
    """Построить картину демо-отеля командой (с приглашениями); вернуть строки для печати."""
    report = await build_showcase(confirmed=confirmed, reset=reset, invite=True, now=now)
    return report.lines


async def build_showcase(
    *, confirmed: bool, reset: bool, invite: bool, now: datetime | None = None
) -> ShowcaseReport:
    """Построить картину демо-отеля — одна на команду и кнопку кабинета (#740).

    `invite=False` — кнопка: приглашения персоналу не выпускаются. `now` — момент
    «сейчас» для границы дня отеля и сроков выезда (тесты сдвигают его на завтра,
    чтобы проверить смену дня); None — настоящее время.
    """
    refusals = staging_refusals(confirmed=confirmed)
    if refusals:
        raise DemoShowcaseError("Это не staging — ничего не сделано: " + "; ".join(refusals) + ".")
    # Два построения разом (кнопка в двух вкладках, кнопка и команда) не строят
    # картину вперемешку: транзакционная блокировка — второе ждёт первое и строит заново.
    async with platform_session_scope() as lock:
        await lock.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _BUILD_LOCK_KEY})
        return await _build_picture(reset=reset, invite=invite, now=now)


async def _build_picture(*, reset: bool, invite: bool, now: datetime | None) -> ShowcaseReport:
    moment = now or utc_now()
    tenant_id = await _demo_tenant_id()
    # Служба «Ресепшен» — раньше настроек: служба эскалаций не должна указывать
    # на службу, которой ещё нет (иначе эскалация — без карточки, spec 0039 §2.3).
    with tenant_context(tenant_id):
        reception_added = await ensure_reception_category()
    async with platform_session_scope() as session:
        config = await mutate_tenant_config(session, tenant_id, _with_demo_settings)
    zone = config.tzinfo
    midnight = datetime.combine(moment.astimezone(zone).date(), time(0), tzinfo=zone).astimezone(
        UTC
    )
    with tenant_context(tenant_id):
        actors, staff_lines = await ensure_staff(tenant_id, invite=invite)
        floors_line = await assign_floors(actors)
        stays_line = await ensure_stays(actors, zone=zone, now=moment, reset=reset)
        turnovers_line = await ensure_turnovers(actors, midnight=midnight, reset=reset)
        requests_line = await ensure_requests(actors, midnight=midnight, reset=reset)
        bookings = await ensure_bookings(
            actors, midnight=midnight, today=moment.astimezone(zone).date(), reset=reset
        )
    overdue_delay = config.reminder_delay_for("maintenance")
    logger.info("demo_showcase_built", tenant_id=str(tenant_id), reset=reset)
    lines = [f"Демо-картина отеля {DEMO_TENANT_SLUG} готова.", "Персонал:", *staff_lines]
    if reception_added:
        lines.append("  Служба «Ресепшен» заведена — без неё ресепшен не пригласить.")
    if config.escalation_category_key == RECEPTION_SERVICE_KEY:
        lines.append(ESCALATION_LINE)
    lines += [floors_line, stays_line, turnovers_line, requests_line, bookings.line]
    if overdue_delay is not None:
        overdue_at = (moment + overdue_delay).astimezone(zone).strftime("%H:%M")
        lines.append(f"Заявка «{OVERDUE_SUMMARY}» станет просроченной около {overdue_at}.")
    if not get_settings().enable_turnover_board:
        lines.append("Внимание: доска выездов выключена (ENABLE_TURNOVER_BOARD) — её не видно.")
    lines.append(SHOW_DAY_HINT)
    new_services = [spec.service for spec in DEMO_REQUESTS if spec.status is RequestStatus.NEW]
    new_services += _restaurant_services(config, bookings.waiting)
    delays = [config.reminder_delay_for(service) for service in new_services]
    return ShowcaseReport(
        lines=lines,
        stays=len(DEMO_STAYS),
        requests=len(DEMO_REQUESTS),
        bookings=bookings.count,
        overdue_after=sorted(delay for delay in delays if delay is not None),
    )


def _restaurant_services(config: TenantConfig, restaurant_keys: tuple[str, ...]) -> list[str]:
    """Службы ресторанов: запрос без ответа — новая заявка в службе своего ресторана."""
    booking = config.table_booking
    restaurants = [booking.restaurant(key) for key in restaurant_keys] if booking else []
    return [restaurant.category_key for restaurant in restaurants if restaurant is not None]


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
