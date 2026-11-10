"""Страница «Тестовые данные» (issue #740): демо-картина смены кнопкой, без SSH.

Нажатие делает то же, что `python -m hospitality.tools.demo_showcase --reset`
(рунбук deploy.md, «Демо-картина к показу»), кроме выпуска приглашений
персоналу: ссылки нужны один раз и остаются за командой. Картина одна на
команду и кнопку (P-12) — `tools/demo_showcase.build_showcase`; действие —
`demo_data_api.py`, страница — `demo_pages.py`.

Где есть — только там, где разрешена команда: все её защиты сразу
(`staging_refusals`: `SENTRY_ENVIRONMENT=staging`, отель контура
`TELEGRAM_TENANT_SLUG` — `demo-hotel`, адрес `PUBLIC_BASE_URL` — `staging.…`),
страница самого демо-отеля и уровень `manager`. У гостевой копии отеля
`SENTRY_ENVIRONMENT` тоже `staging` (#389) — её отсекают отель и адрес контура.
Защиты сервер проверяет на каждом запросе страницы и действия, а не только
прячет пункт меню: чужой контур или отель — 404, как у неизвестного адреса;
уровень ниже менеджера — 403 `ERR-AUTH-003`.
"""

from __future__ import annotations

from hospitality.platform.models import StaffRole
from hospitality.platform.seed import DEMO_TENANT_SLUG
from hospitality.platform.staff_auth import StaffContext
from hospitality.tools.demo_showcase import staging_refusals


def contour_allows(tenant_slug: str) -> bool:
    """Контур и отель — те, где разрешена команда: staging, страница демо-отеля."""
    return tenant_slug == DEMO_TENANT_SLUG and not staging_refusals(confirmed=True)


def nav_visible(staff: StaffContext) -> bool:
    """Пункт меню «Тестовые данные»: контур команды и менеджер — то же, что у страницы."""
    return staff.role_key is StaffRole.MANAGER and contour_allows(staff.tenant_slug)
