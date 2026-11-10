"""Антивирус на пути приёма файла (spec 0075, FOUNDATION §11).

Зовёт только `files.store_upload` — после песочницы и до шифрования, для PDF и
DOCX: их узел хранит как присланы. Картинку проверять незачем: песочница
перерисовала её в WebP, присланные байты выброшены. Адаптер — через порт
`shared/ports/virus_scan.py`; какой он (clamd или Fake), решает composition root.

Антивирус не ответил — файл не принят (Р-5): смысл проверки в том, что
непроверенный файл до бухгалтерии отеля не доходит. Логи — WARNING, не ERROR:
ERROR на пути запроса уносит тело запроса — файл — в Sentry (#433).
"""

from __future__ import annotations

from hospitality.platform.file_content import FilePurpose
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_file_operation
from hospitality.shared.ports.virus_scan import VirusScanUnavailableError, get_virus_scanner

__all__ = ["ERR_FILE_ANTIVIRUS", "ERR_FILE_INFECTED", "scan_for_viruses"]

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8), продолжение ERR-FILES-001…006
# узла файлов (`files.py`).
ERR_FILE_INFECTED = "ERR-FILES-007"
ERR_FILE_ANTIVIRUS = "ERR-FILES-008"

_INFECTED_TEXT = "Файл не прошёл проверку на вирусы. Пришлите фото документа или другой файл."
_RETRY_TEXT = "Не удалось сохранить файл. Попробуйте ещё раз чуть позже."


async def scan_for_viruses(purpose: FilePurpose, data: bytes) -> None:
    """Заражён — ERR-FILES-007 (422); антивирус без вердикта — ERR-FILES-008 (503)."""
    scanner = get_virus_scanner()
    try:
        result = await scanner.scan(data)
    except VirusScanUnavailableError as exc:
        record_file_operation("store", "error", "antivirus")
        logger.warning(
            "file_antivirus_failed",
            error_code=ERR_FILE_ANTIVIRUS,
            purpose=purpose.value,
            backend=scanner.backend,
            detail=str(exc),
        )
        raise AppError(code=ERR_FILE_ANTIVIRUS, message=_RETRY_TEXT, status_code=503) from exc
    if result.infected:
        record_file_operation("store", "rejected", "infected")
        logger.warning(
            "file_infected",
            error_code=ERR_FILE_INFECTED,
            purpose=purpose.value,
            signature=result.signature,
            size_bytes=len(data),
        )
        raise AppError(code=ERR_FILE_INFECTED, message=_INFECTED_TEXT, status_code=422)
