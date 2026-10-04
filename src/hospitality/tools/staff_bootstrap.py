"""Бутстрап первого менеджера тенанта — CLI (spec 0033 §3.3, ADR-008 §1).

Onboarding-шаг платформы (по образцу `tools/checkin`): первого `manager`
создаёт оператор этой командой, дальше персонал приглашается только из UI
кабинета (страница «Сотрудники», PR F серии 0033). Ничего через env или
руками разработчика (§6 FOUNDATION). Пароль запрашивается интерактивно
(getpass) — в аргументах команды и истории shell он не появляется.

Запуск (локально; на staging — то же внутри контейнера, префиксом
`docker compose -f /opt/hospitality/docker-compose.staging.yml exec app`;
`-T` не добавлять: без терминала getpass не гасит эхо и пароль виден на экране):

    python -m hospitality.tools.staff_bootstrap ARUZ --name "Аружан"
    python -m hospitality.tools.staff_bootstrap ARUZ --name "Аружан" \\
        --tenant-slug demo-hotel

Первый аргумент — логин отеля (spec 0037 §2: 3–12 латинских букв и цифр,
первой — буква; регистр не важен). Логин, занятый в этом отеле (учёткой или
ожидающим приглашением — та же проверка, что у приглашения), — отказ:
второй человек в отель приглашается из кабинета, а не повторным бутстрапом.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
import uuid

from sqlalchemy import select

from hospitality.platform.models import (
    StaffRole,
    Tenant,
    TenantMembership,
    User,
    UserIdentity,
    UserIdentityKind,
)
from hospitality.platform.staff_credentials import (
    hash_password,
    password_external_id,
    require_login_format,
)
from hospitality.platform.staff_invites import is_login_taken
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import configure_logging, get_logger

logger = get_logger(module=__name__)


class BootstrapError(Exception):
    """Ошибка ввода оператора: понятный текст вместо трассировки (канон checkin)."""


async def bootstrap_manager(
    tenant_slug: str, staff_login: str, display_name: str, password: str
) -> list[str]:
    """Создать User + UserIdentity(password) с логином отеля + membership
    `manager`; вернуть строки для печати. Одна транзакция; повторный запуск с
    тем же логином в том же отеле — внятный отказ, не дубль. Не тот формат
    логина — ERR-AUTH-012 до всякой работы с БД."""
    staff_login = require_login_format(staff_login)
    secret_hash = await hash_password(password)
    async with platform_session_scope() as session:
        tenant_id: uuid.UUID | None = await session.scalar(
            select(Tenant.id).where(Tenant.slug == tenant_slug)
        )
        if tenant_id is None:
            raise BootstrapError(f"Тенант со slug {tenant_slug!r} не найден (сначала `make seed`).")
        if await is_login_taken(session, tenant_id, staff_login):
            raise BootstrapError(
                f"Логин {staff_login} уже занят в этом отеле. Доступ выдаётся "
                "приглашением из кабинета (страница «Сотрудники»), а не повторным "
                "бутстрапом."
            )
        user = User(display_name=display_name)
        session.add(user)
        await session.flush()
        session.add(
            UserIdentity(
                user_id=user.id,
                kind=UserIdentityKind.PASSWORD,
                external_id=password_external_id(tenant_id, staff_login),
                secret_hash=secret_hash,
            )
        )
        session.add(
            TenantMembership(user_id=user.id, tenant_id=tenant_id, role_key=StaffRole.MANAGER)
        )
    # Логин в лог не пишется (PII) — атрибуция по user_id.
    logger.info(
        "staff.manager_bootstrapped",
        user_id=str(user.id),
        tenant_id=str(tenant_id),
        tenant_slug=tenant_slug,
    )
    return [
        f"Менеджер «{display_name}» создан (user {user.id}).",
        f"Вход в кабинет: /staff/{tenant_slug}/login — логин {staff_login} и заданный пароль.",
        "Дальше сотрудники приглашаются из кабинета (страница «Сотрудники»).",
    ]


def _read_password() -> str:
    password = getpass.getpass("Пароль нового менеджера: ")
    if password != getpass.getpass("Пароль ещё раз: "):
        raise BootstrapError("Пароли не совпали — запустите команду заново.")
    return password


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hospitality.tools.staff_bootstrap",
        description="Первый manager тенанта для кабинета персонала (spec 0033, ADR-008).",
    )
    parser.add_argument(
        "login", help="Логин менеджера в отеле, например BORM (spec 0037, PII_REGISTRY)."
    )
    parser.add_argument("--name", required=True, help="Отображаемое имя (PII_REGISTRY).")
    parser.add_argument(
        "--tenant-slug",
        default=get_settings().telegram_tenant_slug,
        help="Тенант (по умолчанию — TELEGRAM_TENANT_SLUG, как tools/checkin).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI; возвращает код возврата процесса."""
    configure_logging(get_settings().log_level)
    args = _build_parser().parse_args(argv)
    try:
        password = _read_password()
        lines = asyncio.run(bootstrap_manager(args.tenant_slug, args.login, args.name, password))
    except BootstrapError as error:
        print(str(error), file=sys.stderr)
        return 1
    except AppError as error:
        # Ожидаемые отказы ядра (пароль короче минимума и т.п.): текст и код
        # каталога, не трассировка (канон tools/checkin).
        print(f"{error.message} ({error.code}, docs/runbooks/errors.md)", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа; логика покрыта тестами
    raise SystemExit(main())
