"""Адаптер Opera: приём выгрузки отчёта и снимок из неё (spec 0058 §2.4, ADR-004)."""

from __future__ import annotations

from hospitality.integrations.pms.opera_report.adapter import (
    OperaReportPmsAdapter,
    issue_upload_token,
)
from hospitality.integrations.pms.opera_report.router import build_upload_router

__all__ = ["OperaReportPmsAdapter", "build_upload_router", "issue_upload_token"]
