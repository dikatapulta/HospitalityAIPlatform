"""Адаптеры порта бухгалтерии (spec 0052) и выбор адаптера."""

from __future__ import annotations

from hospitality.integrations.accounting.fake import FakeAccountingAdapter
from hospitality.integrations.accounting.onec_xlsx import OneCXlsxAccountingAdapter
from hospitality.shared.ports.accounting import AccountingPort

__all__ = ["FakeAccountingAdapter", "OneCXlsxAccountingAdapter", "build_accounting"]


def build_accounting() -> AccountingPort:
    """Адаптер процесса. Зовёт только composition root.

    Настройки нет: адаптер один и внешней системы не трогает — Excel для 1С
    работает и локально, и на контуре. Второй адаптер (XML обмена 1С или
    настоящая интеграция) — выбор конфигурацией тенанта (FOUNDATION §8 правило 3).
    """
    return OneCXlsxAccountingAdapter()
