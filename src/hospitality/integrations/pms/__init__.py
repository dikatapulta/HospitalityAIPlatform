"""Адаптеры порта PMS (spec 0058 §2.1, ADR-004) и выбор адаптера."""

from __future__ import annotations

from hospitality.integrations.pms.fake import FakePmsAdapter
from hospitality.shared.ports.pms import PmsPort

__all__ = ["FakePmsAdapter", "build_pms"]


def build_pms() -> PmsPort:
    """Адаптер процесса. Зовёт только composition root.

    Пока адаптер один — Fake. Настройка `PMS_BACKEND=fake|opera_report` приходит
    вместе с адаптером Opera (spec 0058 §4, PR 4): настройка без второго значения
    ничего бы не выбирала.
    """
    return FakePmsAdapter()
