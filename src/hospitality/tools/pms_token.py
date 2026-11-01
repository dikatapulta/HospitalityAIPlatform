"""Токен загрузки выгрузки Opera — выпуск и перевыпуск (spec 0058 §2.4, ADR-008).

Запуск на сервере, внутри контейнера `app` (доступ к команде = доступ к серверу):

    python -m hospitality.tools.pms_token <код-отеля>

Печатает новый токен **один раз** — в БД остаётся только его SHA-256. Прежний
токен отеля гаснет сразу: задача выгрузки со старым получает 401 (ERR-PMS-002),
пока IT не впишет новый. Принятый снимок перевыпуск не трогает.

Выпустить токен можно заранее: файлы принимаются, только когда у отеля в
профиле `pms_source: opera_report` (до этого — 409 ERR-PMS-005, файл не
сохраняется). Шаги для IT отеля — `docs/runbooks/opera-report-upload.md`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from sqlalchemy import select

from hospitality.integrations.pms import issue_upload_token
from hospitality.platform.models import Tenant
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.logging import configure_logging, get_logger

logger = get_logger(module=__name__)


class PmsTokenError(Exception):
    """Ошибка ввода оператора: понятный текст вместо трассировки."""


async def issue_for_slug(tenant_slug: str) -> str:
    """Выпустить токен отеля с этим кодом; вернуть plaintext."""
    async with platform_session_scope() as session:
        tenant_id: uuid.UUID | None = await session.scalar(
            select(Tenant.id).where(Tenant.slug == tenant_slug)
        )
    if tenant_id is None:
        raise PmsTokenError(f"Отель с кодом {tenant_slug!r} не найден.")
    token = await issue_upload_token(tenant_id)
    # Токен в лог не пишется: он печатается оператору ровно один раз ниже.
    logger.info("pms_upload_token_issued", tenant_id=str(tenant_id))
    return token


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI; возвращает код возврата процесса."""
    configure_logging(get_settings().log_level)
    parser = argparse.ArgumentParser(
        prog="python -m hospitality.tools.pms_token",
        description="Выпуск токена загрузки выгрузки Opera (прежний токен гаснет).",
    )
    parser.add_argument("tenant_slug", help="Код отеля (slug), например kazakhstan-almaty.")
    args = parser.parse_args(argv)
    try:
        token = asyncio.run(issue_for_slug(args.tenant_slug))
    except PmsTokenError as error:
        print(str(error), file=sys.stderr)
        return 1
    print("Токен загрузки выгрузки Opera (показывается один раз; прежний больше не работает):")
    print(token)
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа; логика покрыта тестами
    raise SystemExit(main())
