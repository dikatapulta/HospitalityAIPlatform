"""Маршрутизация уведомлений по службам: показать/задать маппинг (spec 0026).

Единственный путь записи `TenantConfig.staff_chats_by_category` в Phase 0:
`make seed` заполняет конфиг только у тенанта БЕЗ конфига, а HTTP-эндпоинта
настройки ещё нет (кабинет — Phase 1, §17.7). Живёт в `tools/` — склейке вне
контрактов (как `tools/seed.py`): нужны и kernel (`load/store_tenant_config`),
и модуль requests (проверить, что такая категория у тенанта есть).

Запуск (локально; на staging — то же внутри контейнера, префиксом
`docker compose -f /opt/hospitality/docker-compose.staging.yml exec app`):

    python -m hospitality.tools.staff_routing                       # показать текущий
    python -m hospitality.tools.staff_routing housekeeping=-1001 it-support=-1002
    python -m hospitality.tools.staff_routing --clear               # всё в общий чат

Маппинг задаётся ЦЕЛИКОМ — тем же принципом «конфиг это значение», что и
`store_tenant_config`: перечислили две пары — ровно они и останутся. Неизвестный
ключ категории — отказ (ненулевой код возврата): молча принятая опечатка хуже
отказа, потому что выглядит как рабочая настройка.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

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


class RoutingError(Exception):
    """Ошибка ввода оператора: понятный текст вместо трассировки."""


def parse_pairs(pairs: list[str]) -> dict[str, str]:
    """`["housekeeping=-1001"]` → `{"housekeeping": "-1001"}` (без валидации ключей)."""
    mapping: dict[str, str] = {}
    for pair in pairs:
        category_key, separator, chat_id = pair.partition("=")
        if not separator or not category_key.strip() or not chat_id.strip():
            raise RoutingError(f"Ожидался аргумент вида category-key=chat_id, получено: {pair!r}")
        mapping[category_key.strip()] = chat_id.strip()
    return mapping


async def apply_routing(tenant_slug: str, mapping: dict[str, str] | None) -> dict[str, str]:
    """Показать (`mapping is None`) или записать маппинг тенанта; вернуть итоговый."""
    async with platform_session_scope() as session:
        tenant_id = await session.scalar(select(Tenant.id).where(Tenant.slug == tenant_slug))
        if tenant_id is None:
            raise RoutingError(f"Тенант со slug {tenant_slug!r} не найден (сначала `make seed`).")
        config = await load_tenant_config(session, tenant_id)
        if mapping is None:
            return dict(config.staff_chats_by_category)

        with tenant_context(tenant_id):
            known = {category.key for category in await list_categories()}
        unknown = sorted(set(mapping) - known)
        if unknown:
            raise RoutingError(
                f"Неизвестные категории: {', '.join(unknown)}. "
                f"Доступны: {', '.join(sorted(known)) or '(ни одной — сначала `make seed`)'}"
            )
        # Конфиг — значение: пересобираем целиком и пишем каноническим путём (§6).
        updated = config.model_copy(update={"staff_chats_by_category": mapping})
        await store_tenant_config(session, tenant_id, updated)
        logger.info("staff_routing_updated", tenant_slug=tenant_slug, categories=sorted(mapping))
        return mapping


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hospitality.tools.staff_routing",
        description="Маршрутизация staff-уведомлений: категория заявки → свой чат (spec 0026).",
    )
    parser.add_argument(
        "pairs",
        nargs="*",
        metavar="category-key=chat_id",
        help="Пары «ключ категории = id чата». Без аргументов — показать текущий маппинг.",
    )
    parser.add_argument(
        "--clear", action="store_true", help="Очистить маппинг: все уведомления в общий чат."
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
    if args.clear and args.pairs:
        print("--clear нельзя совмещать с парами: это два разных намерения.", file=sys.stderr)
        return 2
    try:
        mapping = {} if args.clear else (parse_pairs(args.pairs) if args.pairs else None)
        current = asyncio.run(apply_routing(args.tenant_slug, mapping))
    except RoutingError as error:
        print(str(error), file=sys.stderr)
        return 1
    except AppError as error:
        # Ожидаемые отказы ядра (конфиг тенанта не задан / не проходит схему,
        # ERR-PLATFORM-005/006): оператору — текст и код каталога, не трассировка.
        print(f"{error.message} ({error.code}, docs/runbooks/errors.md)", file=sys.stderr)
        return 1
    if not current:
        print("Маппинг пуст: все уведомления идут в TELEGRAM_STAFF_CHAT_ID.")
    else:
        print("Маршрутизация уведомлений (категория → чат):")
        for category_key, chat_id in sorted(current.items()):
            print(f"  {category_key} → {chat_id}")
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа; логика покрыта тестами
    raise SystemExit(main())
