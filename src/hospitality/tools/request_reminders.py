"""Срок напоминания о невзятой заявке: показать/задать (spec 0028, issue #57).

Единственный путь записи `TenantConfig.request_reminder_after_minutes` и
`…_minutes_by_category` в Phase 0: `make seed` заполняет конфиг только у тенанта
БЕЗ конфига, а HTTP-эндпоинта настройки ещё нет (кабинет — Phase 1, §17.7).
Живёт в `tools/` — склейке вне контрактов (канон `tools/staff_routing.py`):
нужны и kernel (`load/store_tenant_config`), и модуль requests (проверить, что
такая категория у тенанта есть).

Запуск (локально; на staging — то же внутри контейнера, префиксом
`docker compose -f /opt/hospitality/docker-compose.staging.yml exec app`):

    python -m hospitality.tools.request_reminders                     # показать текущее
    python -m hospitality.tools.request_reminders --after-minutes 20  # базовый срок
    python -m hospitality.tools.request_reminders maintenance=10 housekeeping=45
    python -m hospitality.tools.request_reminders --off               # выключить совсем

Пер-категорийные сроки задаются ЦЕЛИКОМ — тем же принципом «конфиг это
значение», что и `store_tenant_config`: перечислили две пары — ровно они и
останутся. Неизвестный ключ категории — отказ с ненулевым кодом возврата:
молча принятая опечатка выглядит рабочей настройкой и потому хуже отказа.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from pydantic import ValidationError
from sqlalchemy import select

from hospitality.modules.requests.api import list_categories
from hospitality.platform.config import load_tenant_config, store_tenant_config
from hospitality.platform.models import Tenant
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import configure_logging, get_logger
from hospitality.shared.tenancy import tenant_context

logger = get_logger(module=__name__)


class ReminderConfigError(Exception):
    """Ошибка ввода оператора: понятный текст вместо трассировки."""


def parse_pairs(pairs: list[str]) -> dict[str, int]:
    """`["maintenance=10"]` → `{"maintenance": 10}` (границы проверит схема)."""
    minutes_by_category: dict[str, int] = {}
    for pair in pairs:
        category_key, separator, raw_minutes = pair.partition("=")
        if not separator or not category_key.strip() or not raw_minutes.strip():
            raise ReminderConfigError(
                f"Ожидался аргумент вида category-key=минуты, получено: {pair!r}"
            )
        try:
            minutes = int(raw_minutes.strip())
        except ValueError:
            raise ReminderConfigError(
                f"Минуты должны быть целым числом, получено: {raw_minutes.strip()!r}"
            ) from None
        minutes_by_category[category_key.strip()] = minutes
    return minutes_by_category


async def apply_reminders(
    tenant_slug: str,
    *,
    after_minutes: int | None = None,
    minutes_by_category: dict[str, int] | None = None,
    off: bool = False,
) -> tuple[int | None, dict[str, int]]:
    """Показать (без аргументов правки) или записать сроки; вернуть итоговые.

    `off` обнуляет ОБА поля: «напоминаний у этого отеля нет» без остатков в
    пер-категорийных. `after_minutes` и `minutes_by_category` независимы —
    каждый меняет только своё поле.
    """
    async with platform_session_scope() as session:
        tenant_id = await session.scalar(select(Tenant.id).where(Tenant.slug == tenant_slug))
        if tenant_id is None:
            raise ReminderConfigError(
                f"Тенант со slug {tenant_slug!r} не найден (сначала `make seed`)."
            )
        config = await load_tenant_config(session, tenant_id)
        if not off and after_minutes is None and minutes_by_category is None:
            return config.request_reminder_after_minutes, dict(
                config.request_reminder_minutes_by_category
            )

        if off:
            update: dict[str, object] = {
                "request_reminder_after_minutes": None,
                "request_reminder_minutes_by_category": {},
            }
        else:
            update = {}
            if after_minutes is not None:
                update["request_reminder_after_minutes"] = after_minutes
            if minutes_by_category is not None:
                await _reject_unknown_categories(tenant_id, minutes_by_category)
                update["request_reminder_minutes_by_category"] = minutes_by_category

        # Конфиг — значение: пересобираем целиком и пишем каноническим путём (§6).
        # model_copy схему не перепроверяет, поэтому валидируем явно — иначе
        # срок вне границ попал бы в БД и падал бы уже на чтении.
        try:
            updated = config.model_validate({**config.model_dump(mode="json"), **update})
        except ValidationError as error:
            raise ReminderConfigError(f"Недопустимое значение срока: {error}") from None
        await store_tenant_config(session, tenant_id, updated)
        logger.info(
            "request_reminders_updated",
            tenant_slug=tenant_slug,
            after_minutes=updated.request_reminder_after_minutes,
            categories=sorted(updated.request_reminder_minutes_by_category),
        )
        return updated.request_reminder_after_minutes, dict(
            updated.request_reminder_minutes_by_category
        )


async def _reject_unknown_categories(
    tenant_id: uuid.UUID, minutes_by_category: dict[str, int]
) -> None:
    """Опечатка в ключе категории — отказ со списком доступных (канон 0026)."""
    with tenant_context(tenant_id):
        known = {category.key for category in await list_categories()}
    unknown = sorted(set(minutes_by_category) - known)
    if unknown:
        raise ReminderConfigError(
            f"Неизвестные категории: {', '.join(unknown)}. "
            f"Доступны: {', '.join(sorted(known)) or '(ни одной — сначала `make seed`)'}"
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hospitality.tools.request_reminders",
        description=(
            "Срок, после которого невзятая заявка подсвечивается напоминанием "
            "в чат службы (spec 0028)."
        ),
    )
    parser.add_argument(
        "pairs",
        nargs="*",
        metavar="category-key=минуты",
        help="Пары «ключ категории = минуты». Без аргументов — показать текущее.",
    )
    parser.add_argument(
        "--after-minutes",
        type=int,
        help="Базовый срок в минутах для категорий без своего значения.",
    )
    parser.add_argument(
        "--off", action="store_true", help="Выключить напоминания: обнулить оба поля."
    )
    parser.add_argument(
        "--tenant-slug",
        default=get_settings().telegram_tenant_slug,
        help="Тенант (по умолчанию — TELEGRAM_TENANT_SLUG).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI; возвращает код возврата процесса."""
    configure_logging(get_settings().log_level)
    args = _build_parser().parse_args(argv)
    if args.off and (args.pairs or args.after_minutes is not None):
        print(
            "--off нельзя совмещать со сроками: это два разных намерения.",
            file=sys.stderr,
        )
        return 2
    try:
        pairs = parse_pairs(args.pairs) if args.pairs else None
        after_minutes, by_category = asyncio.run(
            apply_reminders(
                args.tenant_slug,
                after_minutes=args.after_minutes,
                minutes_by_category=pairs,
                off=args.off,
            )
        )
    except ReminderConfigError as error:
        print(str(error), file=sys.stderr)
        return 1
    except AppError as error:
        # Ожидаемые отказы ядра (конфиг тенанта не задан / не проходит схему,
        # ERR-PLATFORM-005/006): оператору — текст и код каталога, не трассировка.
        print(f"{error.message} ({error.code}, docs/runbooks/errors.md)", file=sys.stderr)
        return 1
    if after_minutes is None and not by_category:
        print("Напоминания о невзятых заявках выключены у этого тенанта.")
        return 0
    if after_minutes is None:
        print("Базовый срок: выключен (напоминают только категории ниже).")
    else:
        print(f"Базовый срок: {after_minutes} мин.")
    if by_category:
        print("Свой срок у категорий:")
        for category_key, minutes in sorted(by_category.items()):
            print(f"  {category_key} → {minutes} мин")
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа; логика покрыта тестами
    raise SystemExit(main())
