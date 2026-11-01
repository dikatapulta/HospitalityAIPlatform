"""Адаптеры порта PMS (spec 0058 §2.1, ADR-004) и выбор адаптера."""

from __future__ import annotations

from fastapi import APIRouter

from hospitality.integrations.pms.fake import FakePmsAdapter
from hospitality.integrations.pms.opera_report import (
    OperaReportPmsAdapter,
    build_upload_router,
    issue_upload_token,
)
from hospitality.shared.config import Settings
from hospitality.shared.ports.pms import PmsPort

__all__ = [
    "FakePmsAdapter",
    "OperaReportPmsAdapter",
    "build_pms",
    "build_pms_upload_router",
    "issue_upload_token",
]


def build_pms(settings: Settings, *, max_guests: int) -> PmsPort:
    """Адаптер по `PMS_BACKEND` — зовёт только composition root.

    `max_guests` — потолок гостей одного Stay из домена
    (`guests_api.MAX_GUESTS_PER_STAY`): адаптер Opera отвергает файл с числом
    гостей сверх него, а импортировать модуль `guests` адаптер не вправе
    (контракт 3 import-linter).
    """
    if settings.pms_backend == "opera_report":
        return OperaReportPmsAdapter(max_guests=max_guests)
    return FakePmsAdapter()


def build_pms_upload_router(pms: PmsPort) -> APIRouter | None:
    """Маршрут приёма выгрузки — только у адаптера Opera; у Fake приёма нет (None)."""
    if isinstance(pms, OperaReportPmsAdapter):
        return build_upload_router(pms)
    return None
