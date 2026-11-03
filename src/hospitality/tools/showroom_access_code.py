"""Код доступа 3D-витрины — задать или сменить (spec 0069 §2.2, ADR-025 условие 5).

Запуск на сервере, внутри контейнера `app` (доступ к команде = доступ к серверу):

    python -m hospitality.tools.showroom_access_code set --tenant <код-отеля>

Код читается из переменной окружения `SHOWROOM_ACCESS_CODE`, а если её нет — с
клавиатуры без эха (не попадает в историю оболочки). В БД ложится только bcrypt-хэш;
код не печатается, не пишется в журнал и в лог. Смена кода гасит все прежние cookies
посетителей (версия кода +1). Витрина у отеля должна быть включена онбордингом —
ключ `showroom` в профиле; код не короче 8 знаков.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
import uuid

from sqlalchemy import select

from hospitality.modules.showroom import api as showroom_api
from hospitality.platform.models import Tenant
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.logging import configure_logging, get_logger
from hospitality.shared.tenancy import tenant_context

logger = get_logger(module=__name__)

ENV_VARIABLE = "SHOWROOM_ACCESS_CODE"


class AccessCodeError(Exception):
    """Ошибка ввода оператора: понятный текст вместо трассировки."""


async def set_for_slug(tenant_slug: str, code: str) -> int:
    """Задать код витрины отеля с этим slug; вернуть новую версию кода."""
    try:
        showroom_api.check_access_code_format(code)
    except ValueError as error:
        raise AccessCodeError(f"Код не подходит: {error}.") from error
    async with platform_session_scope() as session:
        tenant_id: uuid.UUID | None = await session.scalar(
            select(Tenant.id).where(Tenant.slug == tenant_slug)
        )
    if tenant_id is None:
        raise AccessCodeError(f"Отель с кодом {tenant_slug!r} не найден.")
    with tenant_context(tenant_id):
        try:
            return await showroom_api.set_access_code(code)
        except ValueError as error:
            raise AccessCodeError(f"Не получилось: {error}.") from error


def _read_code() -> str:
    from_env = os.environ.get(ENV_VARIABLE)
    if from_env:
        return from_env
    return getpass.getpass("Код доступа витрины (не отображается): ")


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI; возвращает код возврата процесса."""
    configure_logging(get_settings().log_level)
    parser = argparse.ArgumentParser(
        prog="python -m hospitality.tools.showroom_access_code",
        description="Код доступа 3D-витрины отеля (прежние cookies посетителей гаснут).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    set_command = commands.add_parser("set", help="Задать или сменить код.")
    set_command.add_argument("--tenant", required=True, help="Код отеля (slug).")
    args = parser.parse_args(argv)
    try:
        version = asyncio.run(set_for_slug(args.tenant, _read_code()))
    except AccessCodeError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"Код доступа витрины задан: версия {version}. Прежние входы посетителей погашены.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
