"""Прогон по эффектам на staging перед обновлением копии отеля (#1051 п. 1).

Проходит путь запуска 01.12 на живом стеке staging — его базе, Redis, настройках демо-отеля
и воркере — и смотрит, куда пришло каждое действие: сотрудник — от приглашения до своей
строки в «Итогах экзаменов», гость — от кода заселения до срочной карточки ЧП, затем
разделы кабинета менеджера. Шаги и их проверки — `tools/effects_run_paths.py`.

Приложение поднимается в этом процессе (`create_app`, как в тестах), запросы идут по его
HTTP разными «браузерами» со своими cookie. Модель — только Fake-адаптеры шлюза: платных
вызовов нет ни одного. Язык и смысл ответа живой модели скрипт не проверяет — это лист
приёмки основателя (`docs/runbooks/founder-acceptance.md`).

Где можно: только staging — те же признаки контура, что у `tools/demo_showcase.py`
(`shared/staging_features.py`), и флаг `--i-know-this-is-staging`. Пишет только в
демо-отель: менеджер и сотрудник прогона (логины `EFM…`/`EFS…` с меткой времени), тестовый
гость в номере `9901`, назначение экзамена службе «Хаускипинг», карточка ЧП. В конце всё
это гасится — и при упавшем шаге: карточка отменена причиной «дубль» (гостю ничего не
уходит), гость выселен, назначение снято, обе учётки отключены. Отключённые учётки и
сданная попытка остаются в истории кабинета, как у любого уволенного сотрудника.

Код возврата: 0 — все шаги ✓; 1 — есть ✗ или «не дошли»; 2 — контур не staging.
Рунбук прогона — `docs/runbooks/effects-run.md`.

    docker compose -f docker-compose.staging.yml --env-file .env exec app \\
        python -m hospitality.tools.effects_run --i-know-this-is-staging
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from hospitality.app import create_app
from hospitality.modules.guests import api as guests_api
from hospitality.modules.mentor import api as mentor_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import load_tenant_config
from hospitality.platform.models import (
    AiRecipient,
    MembershipStatus,
    StaffRole,
    Tenant,
    UserStatus,
)
from hospitality.platform.seed import DEMO_TENANT_SLUG
from hospitality.platform.staff_invites import create_invite, list_pending_invites, revoke_invite
from hospitality.platform.staff_team import (
    deactivate_member,
    list_tenant_members,
    set_member_ai_consents,
)
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import configure_logging, get_logger
from hospitality.shared.staging_features import staging_contour_refusals
from hospitality.shared.tenancy import tenant_context
from hospitality.tools.effects_run_paths import (
    Recorder,
    Run,
    StepResult,
    accept_invite,
    expect,
    guest_path,
    member_id,
    new_password,
    staff_path,
)

logger = get_logger(module=__name__)

CONFIRM_FLAG: Final = "--i-know-this-is-staging"
# Разделы кабинета менеджера, которые есть у копии отеля (#848, шаги 11, 14, 17).
CABINET_PAGES: Final = (
    ("Обзор", ""),
    ("Очередь заявок", "/requests"),
    ("Заселение", "/checkin"),
    ("Сотрудники", "/team"),
    ("Итоги экзаменов", "/mentor/results"),
)
SETUP_STEPS: Final = ("Менеджер прогона входит по приглашению, отметка «Наставник — OpenAI»",)
MARKS: Final = {True: "✓", False: "✗", None: "⬜"}


class EffectsRunError(Exception):
    """Отказ до первого шага: понятный текст оператору вместо трассировки."""


def staging_refusals(*, confirmed: bool) -> list[str]:
    """Почему здесь запускать нельзя; пустой список — это staging."""
    refusals = [] if confirmed else [f"нет флага {CONFIRM_FLAG}"]
    return [*refusals, *staging_contour_refusals(get_settings())]


@dataclass(frozen=True)
class Report:
    version: str
    results: list[StepResult]

    @property
    def passed(self) -> bool:
        return all(result.ok is True for result in self.results)

    def lines(self) -> list[str]:
        lines = [f"Прогон по эффектам · версия {self.version or 'неизвестна'}"]
        for result in self.results:
            detail = f" — {result.detail}" if result.detail else ""
            lines.append(f"{MARKS[result.ok]} {result.path} · {result.title}{detail}")
        failed = sum(result.ok is not True for result in self.results)
        lines.append("Итог: все шаги ✓" if self.passed else f"Итог: не прошли {failed} шагов")
        return lines


async def _demo_tenant_id() -> uuid.UUID:
    async with platform_session_scope() as session:
        tenant_id: uuid.UUID | None = await session.scalar(
            select(Tenant.id).where(Tenant.slug == DEMO_TENANT_SLUG)
        )
    if tenant_id is None:
        raise EffectsRunError(f"Тенанта {DEMO_TENANT_SLUG} нет — сначала tools.seed.")
    return tenant_id


async def _hotel_manager(tenant_id: uuid.UUID) -> uuid.UUID:
    """Действующий менеджер отеля: от его имени выпускается приглашение менеджеру прогона."""
    for member in await list_tenant_members(tenant_id):
        if (
            member.role_key is StaffRole.MANAGER
            and not member.is_platform_admin
            and member.membership_status is MembershipStatus.ACTIVE
            and member.user_status is UserStatus.ACTIVE
        ):
            return member.user_id
    raise EffectsRunError(
        "В демо-отеле нет активного менеджера — сначала tools.staff_bootstrap (deploy.md)."
    )


async def run_effects() -> Report:
    """Пути прогона и уборка; отчёт — по каждому шагу."""
    tenant_id = await _demo_tenant_id()
    inviter_id = await _hotel_manager(tenant_id)
    async with platform_session_scope() as session:
        config = await load_tenant_config(session, tenant_id)
    settings = get_settings()
    origin = settings.public_base_url.rstrip("/")
    app = create_app()
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url=origin) as manager,
        AsyncClient(transport=transport, base_url=origin) as employee,
        AsyncClient(transport=transport, base_url=origin) as guest,
    ):
        run = Run(
            tenant_id=tenant_id,
            slug=DEMO_TENANT_SLUG,
            config=config,
            app=app,
            manager=manager,
            employee=employee,
            guest=guest,
            origin=origin,
            tag=utc_now().strftime("%m%d%H%M"),
        )
        logger.info("effects_run_started", tenant_id=str(tenant_id), tag=run.tag)
        results: list[StepResult] = []
        try:
            setup = await _manager_setup(run, inviter_id)
            results += setup
            if all(result.ok for result in setup):
                results += await staff_path(run)
                results += await guest_path(run)
                results += await cabinet_path(run)
        finally:
            results += await cleanup(run, inviter_id)
    report = Report(version=settings.app_release, results=results)
    log = logger.info if report.passed else logger.warning
    log(
        "effects_run_finished",
        tenant_id=str(tenant_id),
        passed=report.passed,
        failed=[f"{r.path} · {r.title}" for r in results if r.ok is not True],
    )
    return report


async def _manager_setup(run: Run, inviter_id: uuid.UUID) -> list[StepResult]:
    """Менеджер прогона — по приглашению, как любой новый менеджер (пароль знает только он)."""
    rec = Recorder("Подготовка", SETUP_STEPS)
    login = f"EFM{run.tag}"
    try:
        grant = await create_invite(
            run.tenant_id,
            StaffRole.MANAGER,
            f"Проверка запуска · менеджер {run.tag}",
            login,
            invited_by=inviter_id,
        )
        run.created.invite_logins.append(grant.login)
        await accept_invite(run, run.manager, f"/staff/invite/{grant.invite_token}", new_password())
        manager_id = await member_id(run, grant.login)
        run.created.manager_id = manager_id
        # «Итоги экзаменов» — ИИ-раздел: без отметки менеджеру они закрыты (spec 0082).
        await set_member_ai_consents(
            manager_id, run.tenant_id, [AiRecipient.OPENAI], actor_user_id=inviter_id
        )
        rec.ok(f"логин {grant.login}")
    except Exception as error:  # отчёт, а не трассировка: уборка всё равно идёт
        rec.fail(error)
    return rec.results


async def cleanup(run: Run, inviter_id: uuid.UUID) -> list[StepResult]:
    """Погасить созданное прогоном; каждый шаг — сам по себе, сбой одного не держит другие."""
    created, results = run.created, []

    async def step(title: str, hint: str, action: Callable[[], Awaitable[object]]) -> None:
        try:
            await action()
        except Exception as error:  # уборка продолжается; оператор доделает руками по hint
            logger.exception("effects_run_cleanup_failed", step=title)
            results.append(StepResult("Уборка", title, False, f"{error} — {hint}"))
            return
        results.append(StepResult("Уборка", title, True))

    async def in_tenant(call: Awaitable[object]) -> object:
        with tenant_context(run.tenant_id):
            return await call

    if created.card_id is not None:
        card_id = created.card_id
        await step(
            "Карточка ЧП отменена причиной «дубль»",
            "«Очередь заявок» → карточка → «Отменить», «Дубль»",
            lambda: in_tenant(
                requests_api.change_request_status(
                    card_id,
                    requests_api.RequestStatus.CANCELLED,
                    cancel_reason=requests_api.CancelReason.DUPLICATE,
                )
            ),
        )
    if created.stay_id is not None:
        stay_id = created.stay_id
        await step(
            "Тестовый гость выселен",
            "«Заселение» → карточка гостя → «Выселить»",
            lambda: in_tenant(guests_api.check_out(stay_id)),
        )
    if created.assignment_id is not None:
        assignment_id = created.assignment_id
        await step(
            "Назначение экзамена снято",
            "«Итоги экзаменов» → «Снять»",
            lambda: in_tenant(mentor_api.archive_exam_assignment(assignment_id)),
        )
    actor = created.manager_id or inviter_id
    if created.employee_id is not None:
        employee_id = created.employee_id
        await step(
            "Сотрудник прогона отключён",
            "«Сотрудники» → «Отключить»",
            lambda: deactivate_member(employee_id, run.tenant_id, actor_user_id=actor),
        )
    if created.manager_id is not None:
        manager_id = created.manager_id
        await step(
            "Менеджер прогона отключён",
            "«Сотрудники» → «Отключить»",
            lambda: deactivate_member(manager_id, run.tenant_id, actor_user_id=inviter_id),
        )
    await step(
        "Неиспользованные приглашения прогона погашены",
        "«Сотрудники» → приглашение → «Убрать»",
        lambda: _revoke_invites(run, inviter_id),
    )
    return results


async def _revoke_invites(run: Run, inviter_id: uuid.UUID) -> None:
    for invite in await list_pending_invites(run.tenant_id):
        if invite.login in run.created.invite_logins:
            await revoke_invite(invite.invite_id, tenant_id=run.tenant_id, actor_user_id=inviter_id)


async def cabinet_path(run: Run) -> list[StepResult]:
    rec = Recorder("Кабинет", tuple(f"Раздел «{title}» открывается" for title, _ in CABINET_PAGES))
    try:
        for _, path in CABINET_PAGES:
            page = await run.manager.get(f"{run.home}{path}")
            expect(page.status_code == 200, f"ответ {page.status_code}")
            rec.ok()
    except Exception as error:  # отчёт, а не трассировка
        rec.fail(error)
    return rec.results


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hospitality.tools.effects_run",
        description="Прогон по эффектам на staging перед обновлением копии отеля (#1051).",
    )
    parser.add_argument(
        CONFIRM_FLAG, dest="confirmed", action="store_true", help="подтвердить, что это staging"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI; возвращает код возврата процесса."""
    # Отчёт читает человек в терминале, а логи — тот же stdout: INFO-строки приложения
    # утопили бы таблицу. Предупреждения и ошибки (и итог с ✗) остаются.
    configure_logging("WARNING")
    args = _build_parser().parse_args(argv)
    refusals = staging_refusals(confirmed=args.confirmed)
    if refusals:
        print("Запуск только на staging: " + "; ".join(refusals), file=sys.stderr)
        return 2
    try:
        report = asyncio.run(run_effects())
    except EffectsRunError as error:
        print(str(error), file=sys.stderr)
        return 1
    except AppError as error:
        print(f"{error.message} ({error.code}, docs/runbooks/errors.md)", file=sys.stderr)
        return 1
    for line in report.lines():
        print(line)
    return 0 if report.passed else 1


if __name__ == "__main__":  # pragma: no cover — точка входа; логика покрыта тестами
    raise SystemExit(main())
