"""Fake-адаптер антивируса (ADR-007, spec 0075): сигнатуры в памяти процесса.

Ведёт себя как clamd на тех же входах (сверено с ClamAV 1.5.4): тестовый файл
EICAR — файл, который начинается этой строкой и дальше несёт только пробелы, до
128 байт всего, — заражён; та же строка внутри PDF — нет. Свои сигнатуры —
`add_signature`: подстрока байтов, как тело сигнатуры ClamAV, — так тест кладёт
«вирус» в настоящий PDF, который пройдёт проверку типа. `unavailable = True` —
антивирус лежит (контейнера нет, таймаут).
"""

from __future__ import annotations

from hospitality.shared.ports.virus_scan import (
    CLEAN,
    ScanResult,
    VirusScanUnavailableError,
)

# Строка собрана из двух частей, чтобы антивирус машины разработчика не принял
# сам исходник за тестовый вирус.
EICAR_TEST_FILE = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$" + b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
EICAR_SIGNATURE = "Eicar-Test-Signature"
_EICAR_MAX_SIZE = 128


class FakeVirusScanAdapter:
    backend = "fake"

    def __init__(self) -> None:
        self.unavailable = False
        self._signatures: dict[bytes, str] = {}

    def add_signature(self, marker: bytes, name: str) -> None:
        """Файл, содержащий `marker`, — заражён сигнатурой `name`."""
        self._signatures[bytes(marker)] = name

    async def scan(self, data: bytes) -> ScanResult:
        if self.unavailable:
            raise VirusScanUnavailableError("fake antivirus is marked unavailable")
        if _is_eicar_test_file(data):
            return ScanResult(infected=True, signature=EICAR_SIGNATURE)
        for marker, name in self._signatures.items():
            if marker in data:
                return ScanResult(infected=True, signature=name)
        return CLEAN

    async def check(self) -> bool:
        return not self.unavailable


def _is_eicar_test_file(data: bytes) -> bool:
    return (
        len(data) <= _EICAR_MAX_SIZE
        and data.startswith(EICAR_TEST_FILE)
        and not data[len(EICAR_TEST_FILE) :].strip()
    )
