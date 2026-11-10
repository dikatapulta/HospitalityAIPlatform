"""Адаптеры порта антивируса (spec 0075) и выбор по настройкам."""

from __future__ import annotations

from hospitality.integrations.virus_scan.clamd import ClamdVirusScanAdapter
from hospitality.integrations.virus_scan.fake import FakeVirusScanAdapter
from hospitality.shared.config import Settings
from hospitality.shared.ports.virus_scan import VirusScanPort

__all__ = ["ClamdVirusScanAdapter", "FakeVirusScanAdapter", "build_virus_scanner"]


def build_virus_scanner(settings: Settings) -> VirusScanPort:
    """Адаптер по `VIRUS_SCAN_BACKEND` — зовёт только composition root."""
    if settings.virus_scan_backend == "clamav":
        return ClamdVirusScanAdapter(settings.clamav_host, settings.clamav_port)
    return FakeVirusScanAdapter()
