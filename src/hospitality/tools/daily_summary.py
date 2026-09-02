"""Утренняя сводка дня: показать/задать чат и время рассылки (spec 0035 §8).

Единственный путь записи `TenantConfig.daily_summary_chat_id` и
`daily_summary_local_time` в Phase 0: `make seed` заполняет конфиг только у
тенанта БЕЗ конфига, онбординг (`tools.onboard_tenant`) эти два поля намеренно
не трогает — id чата узнают, добавив бота в группу менеджера, а не из файла
профиля (канон `tools/staff_routing.py`). Живёт в `tools/` — склейке вне
контрактов: нужен только kernel (`load/store_tenant_config`).

Запуск (локально; на staging — то же внутри контейнера, префиксом
`docker compose -f /opt/hospitality/docker-compose.staging.yml exec app`):

    python -m hospitality.tools.daily_summary                     # показать текущее
    python -m hospitality.tools.daily_summary --chat-id -1001234  # куда слать
    python -m hospitality.tools.daily_summary --at 08:30          # во сколько
    python -m hospitality.tools.daily_summary --off               # не слать отелю

`--off` выключает сводку ОТЕЛЮ, а не копию основателю: копия уходит в канал
команды и настраивается парой `TELEGRAM_ALERT_*` окружения, а не конфигом
тенанта. Страница «Сводка дня» в кабинете работает независимо от обоих.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from pydantic import ValidationError
from sqlalchemy import select

from hospitality.platform.config import TenantConfig, load_tenant_config, store_tenant_config
from hospitality.platform.models import Tenant
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import configure_logging, get_logger

logger = get_logger(module=__name__)


class DailySummaryConfigError(Exception):
    """Ошибка ввода оператора: понятный текст вместо трассировки."""


async def apply_settings(
    tenant_slug: str, *, chat_id: str | None, local_time: str | None, off: bool
) -> tuple[str | None, str]:
    """Показать или записать настройки сводки; вернуть итоговые (чат, время).

    `chat_id is None and local_time is None and not off` — только показать:
    команда без аргументов ничего не меняет (канон `staff_routing`).
    """
    async with platform_session_scope() as session:
        tenant_id = await session.scalar(select(Tenant.id).where(Tenant.slug == tenant_slug))
        if tenant_id is None:
            raise DailySummaryConfigError(
                f"Тенант со slug {tenant_slug!r} не найден (сначала `make seed`)."
            )
        config = await load_tenant_config(session, tenant_id)
        if chat_id is None and local_time is None and not off:
            return config.daily_summary_chat_id, config.daily_summary_local_time

        # Конфиг — значение: пересобираем целиком и пишем каноническим путём (§6).
        # Через `model_validate`, а не `model_copy(update=...)`: второй схему НЕ
        # перепроверяет, и "9:00" или чат из одних пробелов легли бы в БД молча,
        # а упали бы при следующем чтении конфига — уже у воркера.
        payload = config.model_dump()
        if off:
            payload["daily_summary_chat_id"] = None
        elif chat_id is not None:
            payload["daily_summary_chat_id"] = chat_id
        if local_time is not None:
            payload["daily_summary_local_time"] = local_time
        try:
            updated = TenantConfig.model_validate(payload)
        except ValidationError as error:
            raise DailySummaryConfigError(f"Негодное значение:\n{error}") from error
        await store_tenant_config(session, tenant_id, updated)
        logger.info(
            "daily_summary_config_updated",
            tenant_slug=tenant_slug,
            chat_configured=updated.daily_summary_chat_id is not None,
            local_time=updated.daily_summary_local_time,
        )
        return updated.daily_summary_chat_id, updated.daily_summary_local_time


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hospitality.tools.daily_summary",
        description="Утреннее сообщение со сводкой дня: чат менеджера и время (spec 0035 §8).",
    )
    parser.add_argument("--chat-id", help="Внешний id чата менеджера (Telegram chat.id строкой).")
    parser.add_argument("--at", dest="local_time", help="Время рассылки HH:MM по времени отеля.")
    parser.add_argument(
        "--off", action="store_true", help="Не слать сводку отелю (страница кабинета остаётся)."
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
    if args.off and args.chat_id:
        print("--off нельзя совмещать с --chat-id: это два разных намерения.", file=sys.stderr)
        return 2
    try:
        chat_id, local_time = asyncio.run(
            apply_settings(
                args.tenant_slug, chat_id=args.chat_id, local_time=args.local_time, off=args.off
            )
        )
    except DailySummaryConfigError as error:
        print(str(error), file=sys.stderr)
        return 1
    except AppError as error:
        # Ожидаемые отказы ядра (конфиг тенанта не задан / не проходит схему,
        # ERR-PLATFORM-005/006): оператору — текст и код каталога, не трассировка.
        print(f"{error.message} ({error.code}, docs/runbooks/errors.md)", file=sys.stderr)
        return 1
    if chat_id is None:
        print("Сводка отелю не уходит: чат не задан (страница кабинета работает).")
    else:
        print(f"Сводка уходит в чат {chat_id} в {local_time} по времени отеля.")
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа; логика покрыта тестами
    raise SystemExit(main())
