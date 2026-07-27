"""Заселение гостя и выдача кода — CLI до кабинета персонала (spec 0027 §1.6).

Кнопка «заселить» появится в кабинете v1 (#48); до неё Stay создаёт
основатель/ресепшен этой командой (тот же приём, что `tools/staff_routing`).
Код заселения печатается РОВНО ОДИН РАЗ — в БД только хэш; потерян —
`--reissue` (ADR-008 §3).

Запуск (локально; на staging — `docker compose exec app <то же>`):

    python -m hospitality.tools.checkin 101 --guest "Wang Li" --nights 2
    python -m hospitality.tools.checkin 101 --check-out "2026-07-29 12:00"
    python -m hospitality.tools.checkin 101 --reissue        # гость потерял код
    python -m hospitality.tools.checkin 101 --check-out-now  # ранний выезд
    python -m hospitality.tools.checkin --list               # активные Stay

`--check-out` и дефолт «12:00 через N ночей» — ЛОКАЛЬНОЕ время отеля (пояс из
конфига тенанта, §9); в БД уходит UTC.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from hospitality.modules.guests.api import (
    StayCheckIn,
    check_in,
    check_out,
    find_active_stay,
    format_access_code,
    list_active_stays,
    reissue_access_code,
)
from hospitality.platform.config import load_tenant_config
from hospitality.platform.models import Tenant
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import configure_logging
from hospitality.shared.tenancy import tenant_context

# Час выезда по умолчанию (локальное время отеля) для `--nights`.
_DEFAULT_CHECKOUT_HOUR = 12


class CheckinError(Exception):
    """Ошибка ввода оператора: понятный текст вместо трассировки."""


async def _resolve_tenant(tenant_slug: str) -> tuple[uuid.UUID, ZoneInfo]:
    """(tenant_id, часовой пояс отеля) по slug; тенант обязан быть настроен."""
    async with platform_session_scope() as session:
        tenant_id: uuid.UUID | None = await session.scalar(
            select(Tenant.id).where(Tenant.slug == tenant_slug)
        )
        if tenant_id is None:
            raise CheckinError(f"Тенант со slug {tenant_slug!r} не найден (сначала `make seed`).")
        config = await load_tenant_config(session, tenant_id)
    return tenant_id, config.tzinfo


def _parse_check_out(raw: str | None, nights: int, zone: ZoneInfo, *, now: datetime) -> datetime:
    """Момент выезда в UTC: явный `--check-out` (локальное время) или `--nights`."""
    if raw is not None:
        try:
            local = datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=zone)
        except ValueError:
            raise CheckinError(
                f"Ожидался формат 'YYYY-MM-DD HH:MM' (локальное время отеля), получено: {raw!r}"
            ) from None
    else:
        local_today = now.astimezone(zone).date()
        local = datetime.combine(
            local_today + timedelta(days=nights),
            time(hour=_DEFAULT_CHECKOUT_HOUR),
            tzinfo=zone,
        )
    utc_value = local.astimezone(ZoneInfo("UTC"))
    if utc_value <= now:
        raise CheckinError(f"Выезд {raw or local.isoformat()} уже в прошлом — проверьте дату.")
    return utc_value


async def _run(args: argparse.Namespace) -> list[str]:
    """Выполнить команду; вернуть строки для печати."""
    tenant_id, zone = await _resolve_tenant(args.tenant_slug)
    with tenant_context(tenant_id):
        if args.list:
            stays = await list_active_stays()
            if not stays:
                return ["Активных проживаний нет."]
            return [
                f"  {stay.room_number}: до "
                f"{stay.check_out_at.astimezone(zone).strftime('%Y-%m-%d %H:%M')} "
                f"(stay {stay.id})"
                for stay in stays
            ]

        assert args.room is not None  # argparse гарантирует (см. main)
        if args.reissue or args.check_out_now:
            stay = await find_active_stay(args.room)
            if stay is None:
                raise CheckinError(f"Активного проживания в комнате {args.room!r} нет.")
            if args.reissue:
                code = await reissue_access_code(stay.id)
                return [
                    f"Новый код заселения для комнаты {stay.room_number}: "
                    f"{format_access_code(code)}",
                    "Старый код погашен; уже привязанные устройства продолжают работать.",
                ]
            closed = await check_out(stay.id)
            return [f"Комната {closed.room_number}: выезд оформлен, доступ гостя закрыт."]

        result = await check_in(
            StayCheckIn(
                room_number=args.room,
                check_out_at=_parse_check_out(args.check_out, args.nights, zone, now=utc_now()),
                guest_display_name=args.guest,
            )
        )
        local_out = result.stay.check_out_at.astimezone(zone).strftime("%Y-%m-%d %H:%M")
        return [
            f"Комната {result.stay.room_number}: гость заселён до {local_out}.",
            f"КОД ЗАСЕЛЕНИЯ (показывается один раз): {format_access_code(result.access_code)}",
            "Передайте код гостю. Потерян — перевыпуск: --reissue.",
        ]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hospitality.tools.checkin",
        description="Заселение/выезд гостя и код веб-чата (spec 0027, ADR-008).",
    )
    parser.add_argument("room", nargs="?", help="Номер комнаты (например, 101).")
    parser.add_argument("--guest", help="Имя гостя (необязательно; PII_REGISTRY).")
    parser.add_argument(
        "--nights", type=int, default=1, help="Ночей до выезда в 12:00 (по умолчанию 1)."
    )
    parser.add_argument(
        "--check-out", help="Момент выезда 'YYYY-MM-DD HH:MM' (локальное время отеля)."
    )
    parser.add_argument("--reissue", action="store_true", help="Перевыпустить код заселения.")
    parser.add_argument(
        "--check-out-now", action="store_true", help="Оформить выезд сейчас (доступ гаснет)."
    )
    parser.add_argument("--list", action="store_true", help="Показать активные проживания.")
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
    if not args.list and args.room is None:
        print("Укажите номер комнаты или --list.", file=sys.stderr)
        return 2
    if args.reissue and args.check_out_now:
        print("--reissue нельзя совмещать с --check-out-now.", file=sys.stderr)
        return 2
    try:
        lines = asyncio.run(_run(args))
    except CheckinError as error:
        print(str(error), file=sys.stderr)
        return 1
    except AppError as error:
        # Ожидаемые отказы ядра (комната занята, конфиг тенанта не задан):
        # оператору — текст и код каталога, не трассировка.
        print(f"{error.message} ({error.code}, docs/runbooks/errors.md)", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа; логика покрыта тестами
    raise SystemExit(main())
