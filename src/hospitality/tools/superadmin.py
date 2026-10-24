"""Суперадмин на сервере — CLI (spec 0044 §8, рунбук `docs/runbooks/superadmin.md`).

Три команды, все — только по SSH-ключу на сервере (доступ к команде = доступ к
серверу), внутри контейнера `app`, БЕЗ `-T`: без терминала getpass не гасит эхо.

    python -m hospitality.tools.superadmin create ADMN --name "Имя" --tenant-slug <код>
    python -m hospitality.tools.superadmin reset-factor ADMN --tenant-slug <код>
    python -m hospitality.tools.superadmin set-password ADMN --tenant-slug <код>

`create` и `reset-factor` показывают QR кода из приложения и ключ текстом и
пишут в БД только после верного кода из приложения: неправильно отсканированный
QR не запирает основателя. Принятый при подтверждении шаг запоминается — тот же
код второй раз на входе не подойдёт. `reset-factor` (потеря телефона) и
`set-password` гасят все сессии суперадмина. Каждая команда — уведомление
основателю (`SuperadminActed`, в одной транзакции с записью).

Пароль — getpass дважды, в аргументах и истории shell его нет (канон
`staff_bootstrap`). Логин — PII: в лог не пишется, атрибуция по user_id.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
import uuid
from collections.abc import Callable

import segno
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.platform.events import SuperadminActed, SuperadminActionKind
from hospitality.platform.models import (
    StaffRole,
    StaffSecondFactor,
    StaffSession,
    Tenant,
    TenantMembership,
    User,
    UserIdentity,
    UserIdentityKind,
    UserStatus,
)
from hospitality.platform.staff_credentials import (
    hash_password,
    password_external_id,
    require_login_format,
)
from hospitality.platform.staff_invites import is_login_taken
from hospitality.platform.totp import (
    generate_secret,
    grouped_secret,
    matching_step,
    provisioning_uri,
)
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.events import publish
from hospitality.shared.logging import configure_logging, get_logger
from hospitality.shared.tenancy import tenant_context

logger = get_logger(module=__name__)

# Показать QR и ключ, вернуть введённый код. Инъекция — тесты подставляют
# «приложение», которое само считает код из секрета.
AskCode = Callable[[str, str], str]


class SuperadminError(Exception):
    """Ошибка ввода оператора: понятный текст вместо трассировки (канон checkin)."""


def _bad_code() -> SuperadminError:
    return SuperadminError(
        "Код не подошёл — ничего не записано. Проверьте, что время на телефоне "
        "ставится автоматически, и запустите команду заново."
    )


async def _tenant_id(tenant_slug: str) -> uuid.UUID:
    async with platform_session_scope() as session:
        tenant_id: uuid.UUID | None = await session.scalar(
            select(Tenant.id).where(Tenant.slug == tenant_slug)
        )
    if tenant_id is None:
        raise SuperadminError(f"Отель с кодом {tenant_slug!r} не найден.")
    return tenant_id


def _confirmed_factor(staff_login: str, tenant_slug: str, ask_code: AskCode) -> tuple[str, int]:
    """Новый секрет, показ QR и подтверждение кодом; (секрет, принятый шаг)."""
    secret = generate_secret()
    uri = provisioning_uri(secret, account=f"{staff_login} ({tenant_slug})")
    step = matching_step(secret, ask_code(secret, uri), now=utc_now(), last_used_step=None)
    if step is None:
        raise _bad_code()
    return secret, step


async def _find_superadmin(tenant_id: uuid.UUID, staff_login: str) -> uuid.UUID:
    async with platform_session_scope() as session:
        user_id: uuid.UUID | None = await session.scalar(
            select(User.id)
            .join(UserIdentity, UserIdentity.user_id == User.id)
            .where(
                UserIdentity.kind == UserIdentityKind.PASSWORD,
                UserIdentity.external_id == password_external_id(tenant_id, staff_login),
                User.is_platform_admin.is_(True),
                User.status != UserStatus.ERASED,
            )
        )
    if user_id is None:
        raise SuperadminError(f"Суперадмина с логином {staff_login} в этом отеле нет.")
    return user_id


def _entry_url(tenant_slug: str) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/staff/{tenant_slug}/superadmin"


async def create_superadmin(
    tenant_slug: str, staff_login: str, display_name: str, password: str, ask_code: AskCode
) -> list[str]:
    """Завести суперадмина: User с флагом + логин отеля + членство `manager` +
    второй фактор — одной транзакцией, только после верного кода."""
    staff_login = require_login_format(staff_login)
    secret_hash = await hash_password(password)
    tenant_id = await _tenant_id(tenant_slug)
    async with platform_session_scope() as session:
        taken = await is_login_taken(session, tenant_id, staff_login)
    if taken:
        raise SuperadminError(f"Логин {staff_login} уже занят в этом отеле — выберите другой.")
    secret, step = _confirmed_factor(staff_login, tenant_slug, ask_code)
    with tenant_context(tenant_id):
        async with session_scope() as session:
            if await is_login_taken(session, tenant_id, staff_login):
                raise SuperadminError(f"Логин {staff_login} заняли, пока шла команда.")
            user = User(display_name=display_name, is_platform_admin=True)
            session.add(user)
            await session.flush()
            session.add_all(
                [
                    UserIdentity(
                        user_id=user.id,
                        kind=UserIdentityKind.PASSWORD,
                        external_id=password_external_id(tenant_id, staff_login),
                        secret_hash=secret_hash,
                    ),
                    TenantMembership(
                        user_id=user.id, tenant_id=tenant_id, role_key=StaffRole.MANAGER
                    ),
                    StaffSecondFactor(user_id=user.id, totp_secret=secret, last_used_step=step),
                ]
            )
            await _announce(session, SuperadminActionKind.CREATED_ON_SERVER, user.id)
    logger.info("staff.superadmin_created", user_id=str(user.id), tenant_id=str(tenant_id))
    return [
        f"Суперадмин «{display_name}» заведён.",
        f"Вход: {_entry_url(tenant_slug)} — логин {staff_login}, пароль и код из приложения.",
        "Сохраните адрес в закладки: на обычной странице входа суперадмин не войдёт.",
    ]


async def reset_factor(tenant_slug: str, staff_login: str, ask_code: AskCode) -> list[str]:
    """Потеря телефона: новый секрет после верного кода, сессии погашены."""
    staff_login = require_login_format(staff_login)
    tenant_id = await _tenant_id(tenant_slug)
    user_id = await _find_superadmin(tenant_id, staff_login)
    secret, step = _confirmed_factor(staff_login, tenant_slug, ask_code)
    with tenant_context(tenant_id):
        async with session_scope() as session:
            factor = await session.get(StaffSecondFactor, user_id, with_for_update=True)
            if factor is None:
                session.add(
                    StaffSecondFactor(user_id=user_id, totp_secret=secret, last_used_step=step)
                )
            else:
                factor.totp_secret = secret
                factor.last_used_step = step
            await _revoke_sessions(session, user_id)
            await _announce(session, SuperadminActionKind.FACTOR_RESET_ON_SERVER, user_id)
    logger.info("staff.superadmin_factor_reset", user_id=str(user_id), tenant_id=str(tenant_id))
    return [
        "Код из приложения заменён: прежний телефон больше не подходит.",
        f"Все сессии суперадмина закрыты — войдите заново: {_entry_url(tenant_slug)}",
    ]


async def set_password(tenant_slug: str, staff_login: str, password: str) -> list[str]:
    """Забытый пароль суперадмина: новый хэш той же идентичности, сессии погашены."""
    staff_login = require_login_format(staff_login)
    secret_hash = await hash_password(password)
    tenant_id = await _tenant_id(tenant_slug)
    user_id = await _find_superadmin(tenant_id, staff_login)
    with tenant_context(tenant_id):
        async with session_scope() as session:
            await session.execute(
                update(UserIdentity)
                .where(
                    UserIdentity.kind == UserIdentityKind.PASSWORD,
                    UserIdentity.external_id == password_external_id(tenant_id, staff_login),
                )
                .values(secret_hash=secret_hash)
            )
            await _revoke_sessions(session, user_id)
            await _announce(session, SuperadminActionKind.PASSWORD_SET_ON_SERVER, user_id)
    logger.info("staff.superadmin_password_set", user_id=str(user_id), tenant_id=str(tenant_id))
    return [f"Пароль суперадмина сменён, сессии закрыты: {_entry_url(tenant_slug)}"]


async def _revoke_sessions(session: AsyncSession, user_id: uuid.UUID) -> None:
    await session.execute(
        update(StaffSession)
        .where(StaffSession.user_id == user_id, StaffSession.revoked_at.is_(None))
        .values(revoked_at=utc_now())
    )


async def _announce(session: AsyncSession, kind: SuperadminActionKind, user_id: uuid.UUID) -> None:
    await publish(session, SuperadminActed(kind=kind, occurred_at=utc_now(), actor_user_id=user_id))


def _ask_code_in_terminal(secret: str, uri: str) -> str:
    """QR в терминал (segno), ключ текстом и запрос кода."""
    segno.make(uri, error="m").terminal(compact=True)
    print(
        "Отсканируйте QR приложением-аутентификатором (Google Authenticator и аналоги).\n"
        f"Не сканируется — добавьте ключ вручную: {grouped_secret(secret)}"
    )
    return input("Код из приложения: ")


def _read_password(prompt: str) -> str:
    password = getpass.getpass(prompt)
    if password != getpass.getpass("Пароль ещё раз: "):
        raise SuperadminError("Пароли не совпали — запустите команду заново.")
    return password


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hospitality.tools.superadmin",
        description="Суперадмин кабинета: заведение, новый код из приложения, новый пароль "
        "(spec 0044, docs/runbooks/superadmin.md).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("create", "Завести суперадмина (пароль, затем QR и код из приложения)."),
        ("reset-factor", "Потеря телефона: новый QR, прежний код перестаёт работать."),
        ("set-password", "Забытый пароль суперадмина: задать новый."),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("login", help="Логин суперадмина в отеле (spec 0037 §2).")
        command.add_argument(
            "--tenant-slug",
            default=get_settings().telegram_tenant_slug,
            help="Код отеля (по умолчанию — TELEGRAM_TENANT_SLUG, как staff_bootstrap).",
        )
        if name == "create":
            command.add_argument("--name", required=True, help="Отображаемое имя (PII_REGISTRY).")
    return parser


async def _run(args: argparse.Namespace) -> list[str]:
    if args.command == "create":
        password = _read_password("Пароль суперадмина: ")
        return await create_superadmin(
            args.tenant_slug, args.login, args.name, password, _ask_code_in_terminal
        )
    if args.command == "reset-factor":
        return await reset_factor(args.tenant_slug, args.login, _ask_code_in_terminal)
    password = _read_password("Новый пароль суперадмина: ")
    return await set_password(args.tenant_slug, args.login, password)


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI; возвращает код возврата процесса."""
    configure_logging(get_settings().log_level)
    args = _build_parser().parse_args(argv)
    try:
        lines = asyncio.run(_run(args))
    except SuperadminError as error:
        print(str(error), file=sys.stderr)
        return 1
    except AppError as error:
        # Ожидаемые отказы ядра (формат логина, короткий пароль): текст и код
        # каталога, не трассировка (канон staff_bootstrap).
        print(f"{error.message} ({error.code}, docs/runbooks/errors.md)", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа; логика покрыта тестами
    raise SystemExit(main())
