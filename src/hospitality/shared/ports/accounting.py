"""Порт бухгалтерии (FOUNDATION §8 — `AccountingPort`, P-3; spec 0052, issue #517).

Канон порта — `shared/ports/file_storage.py`: `typing.Protocol` с async-методами,
адаптеры — `integrations/accounting/` (Fake обязателен, ADR-007), общий набор
контрактных тестов (`tests/test_accounting_port.py`, R-7), адаптер выбирает
только composition root (`app.py`) и кладёт сюда `set_accounting`.

Сейчас порт умеет одно: отдать контрагентов файлом, который бухгалтер отеля
загрузит в свою учётную систему (§8 правило 5 — упрощённый адаптер вместо
настоящей интеграции с 1С). Формат файла, заголовки и имя — дело адаптера:
ядро передаёт строки и период и не знает, что за ними — Excel для 1С или иной.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class CounterpartyRow(BaseModel):
    """Контрагент для учётной системы и проживание, к которому он относится.

    Реквизиты — проверенные человеком значения (spec 0049 Р-5), пусто — поле не
    заполнено. Номер и даты — подсказка бухгалтеру, к какому проживанию АВР.
    """

    model_config = ConfigDict(frozen=True)

    company_name: str
    bin_iin: str
    address: str
    iik: str
    bik: str
    bank_name: str
    room_number: str
    check_in: date
    check_out: date


class ExportFile(BaseModel):
    """Файл выгрузки: имя для «Сохранить как», тип и байты."""

    model_config = ConfigDict(frozen=True)

    filename: str
    content_type: str
    content: bytes


class AccountingNotConfiguredError(RuntimeError):
    """Composition root не положил адаптер — программная ошибка сборки процесса."""


class AccountingPort(Protocol):
    """Учётная система отеля (1С и подобные).

    Контракт (общие тесты — `tests/test_accounting_port.py`):
    - `export_counterparties` отдаёт файл: строка заголовка и по строке на
      контрагента в переданном порядке, ничего сверх; пустой список — только
      заголовок;
    - значения — текст как есть: не формула, ведущие нули БИН/ИИН целы;
      управляющие символы, которых не допускает формат, выброшены;
    - имя файла содержит период и кончается расширением формата;
    - `check` — health адаптера (§8 правило 1): `True`, если готов выгружать.
    """

    backend: str

    async def export_counterparties(
        self, rows: Sequence[CounterpartyRow], *, period_from: date, period_to: date
    ) -> ExportFile: ...

    async def check(self) -> bool: ...


_accounting: AccountingPort | None = None


def set_accounting(accounting: AccountingPort) -> None:
    """Положить адаптер процесса. Зовёт только composition root (и тесты)."""
    global _accounting
    _accounting = accounting


def get_accounting() -> AccountingPort:
    """Адаптер процесса; не положен — `AccountingNotConfiguredError`."""
    if _accounting is None:
        raise AccountingNotConfiguredError(
            "accounting is not configured: composition root must call set_accounting()"
        )
    return _accounting


def accounting_or_none() -> AccountingPort | None:
    """То же без исключения — тестам, которые возвращают прежний адаптер."""
    return _accounting
