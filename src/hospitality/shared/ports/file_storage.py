"""CANONICAL: порт ядра (P-3, FOUNDATION §8, ADR-007) — хранилище файлов (spec 0047, ADR-022).

Канон порта, первого в проекте:

- порт — `typing.Protocol` в `shared/ports/<домен>.py`: ядро объявляет, что ему нужно,
  и не знает, кто это делает;
- ошибки порта — его собственные исключения рядом с ним: адаптер переводит в них ошибки
  своей системы, ядро ловит только их;
- адаптеры — `integrations/<домен>/`: обязательный `fake.py` с `Fake<Port>Adapter` (ADR-007)
  и production-адаптеры; оба проходят один контрактный набор тестов
  (`tests/test_file_storage_contract.py`, R-7);
- подключает адаптер только composition root (`app.py`, `worker.py`; контракт 2
  import-linter).

Порт хранит непрозрачные байты по ключу. Шифрование, проверка типа и метаданные — забота
сервиса ядра `platform/files.py`, поэтому тип содержимого порт не принимает: на томе лежит
шифротекст (ADR-022 п. 2).
"""

from __future__ import annotations

import re
from typing import Protocol

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# `{tenant_id}/{purpose}/{file_id}.{ext}` — тенант в ключе: вторая стена к RLS метаданных.
# Строгий формат и есть защита от обхода каталога: ни `..`, ни `/` в начале сюда не пройдут.
_STORAGE_KEY = re.compile(rf"^{_UUID}/[a-z][a-z_]{{0,31}}/{_UUID}\.[a-z0-9]{{1,8}}$")


class StoredObjectNotFoundError(Exception):
    """По ключу ничего нет: не записывали, удалили или том потерян."""


class FileStorageError(Exception):
    """Хранилище не выполнило операцию (диск, права, сеть) — не путать с «нет такого ключа»."""


def validate_storage_key(key: str) -> None:
    """Негодный ключ — ошибка программиста: `ValueError` до любого обращения к хранилищу."""
    if not _STORAGE_KEY.fullmatch(key):
        raise ValueError(f"storage key must be '<tenant>/<purpose>/<uuid>.<ext>', got {key!r}")


class FileStoragePort(Protocol):
    """Байты по ключу. Каждый метод сначала проверяет ключ (`validate_storage_key`)."""

    async def put(self, key: str, data: bytes) -> None:
        """Записать байты; существующий ключ перезаписывается."""

    async def get(self, key: str) -> bytes:
        """Прочитать байты; нет ключа — `StoredObjectNotFoundError`."""
        ...

    async def delete(self, key: str) -> None:
        """Удалить; отсутствующий ключ — не ошибка (повтор безопасен, P-8)."""

    async def check_health(self) -> bool:
        """Хранилище готово принимать запись — для `/health/ready` (§8 правило 1)."""
        ...
