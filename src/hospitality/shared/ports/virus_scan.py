"""Порт антивируса для принятых файлов (spec 0075, FOUNDATION §11, P-3).

Устроен по канону `shared/ports/file_storage.py`: `typing.Protocol` с
async-методами, исключение порта рядом, адаптеры — `integrations/virus_scan/`
(Fake обязателен, ADR-007), общий контрактный набор на все адаптеры, выбор
адаптера — только composition root (`app.py`), который кладёт его сюда
`set_virus_scanner`.

Порт видит непрозрачные байты и отвечает вердиктом: о тенантах, назначениях и
типах файлов он не знает — что проверять, решает сервис `platform/files.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class VirusScanUnavailableError(Exception):
    """Антивирус не дал вердикта: нет соединения, таймаут, ошибка на его стороне."""


class VirusScannerNotConfiguredError(RuntimeError):
    """Composition root не положил адаптер — программная ошибка сборки процесса."""


@dataclass(frozen=True)
class ScanResult:
    """Вердикт антивируса; `signature` — имя найденной сигнатуры, у чистого — None."""

    infected: bool
    signature: str | None = None


CLEAN = ScanResult(infected=False)


class VirusScanPort(Protocol):
    """Проверка байтов антивирусом.

    Контракт (общие тесты — `tests/test_virus_scan_port.py`):
    - `scan` чистого файла — `ScanResult(infected=False)`; файла с тестовой
      строкой EICAR — `infected=True` и имя сигнатуры;
    - нет вердикта — `VirusScanUnavailableError`, а не «чисто»: вызывающий
      решает сам, принимать ли файл без проверки (узел файлов — нет);
    - `check` — health адаптера (FOUNDATION §8 правило 1): `True`, если отвечает.
    """

    backend: str

    async def scan(self, data: bytes) -> ScanResult: ...

    async def check(self) -> bool: ...


_scanner: VirusScanPort | None = None


def set_virus_scanner(scanner: VirusScanPort) -> None:
    """Положить адаптер процесса. Зовёт только composition root (и тесты)."""
    global _scanner
    _scanner = scanner


def get_virus_scanner() -> VirusScanPort:
    """Адаптер процесса; не положен — `VirusScannerNotConfiguredError`."""
    if _scanner is None:
        raise VirusScannerNotConfiguredError(
            "virus scanner is not configured: composition root must call set_virus_scanner()"
        )
    return _scanner


def virus_scanner_or_none() -> VirusScanPort | None:
    """То же без исключения — тестам, которым нужно вернуть прежний адаптер."""
    return _scanner
