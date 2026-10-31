"""Место на диске сервера — седьмая линия алертера (issue #414, ERR-OPS-012).

На staging-сервере одна файловая система держит всё, что растёт: образы docker,
логи контейнеров, бэкапы и снимки перед миграцией, тома Postgres обоих стеков
(staging и гостевой копии). 31.10.2026 она заполнилась на 100%, и база гостевой
копии перестала писать — узнали случайно, а не от сторожа. Эта линия говорит
заранее: свободного места меньше доли ``ALERT_DISK_MIN_FREE_RATIO``.

Смотреть надо глазами хоста, а у алертера из файловой системы хоста есть только
каталог бэкапов, смонтированный для чтения (issue #106). Он лежит на той же ФС,
поэтому ``os.statvfs`` по нему даёт заполнение всего диска без новых монтирований;
путь задаёт ``ALERT_DISK_PATH`` (в staging-стеке — ``/backups``). Отдельная
настройка, а не ``ALERT_BACKUP_DIR``: линии включаются и выключаются порознь,
и тест бэкапа не зависит от того, сколько места на машине, где он идёт.

Свободное — то, что доступно непривилегированному процессу (``f_bavail``), как
Postgres в контейнере: блоки, зарезервированные за root, ему не достанутся.
``✅`` — когда свободного стало на ``_RECOVERY_MARGIN`` больше порога: логи
пишутся и ротируются непрерывно, и без запаса заполнение на самой границе
давало бы пару «🔴/✅» каждые несколько минут.

«Не знаю» (``statvfs`` не прошёл — пути нет) — молчание, как у линий по
метрикам: путь — тот же каталог бэкапов, и его пропажу уже называет ERR-OPS-008.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Код каталога (docs/runbooks/errors.md, R-8).
ERR_DISK_LOW = "ERR-OPS-012"

# Запас выхода из алерта — доля диска сверх порога (2 п.п. ≈ 0,8 ГБ на 40 ГБ).
_RECOVERY_MARGIN = 0.02

_GIB = 1024**3


@dataclass(frozen=True)
class DiskProbe:
    """Место на файловой системе пути на момент опроса (issue #414)."""

    path: str
    free_bytes: int
    total_bytes: int

    @property
    def free_ratio(self) -> float:
        return self.free_bytes / self.total_bytes


def probe_disk(path: str) -> DiskProbe | None:
    """Свободное и всё место ФС, на которой лежит ``path``.

    None — линия выключена (пустой путь: dev, CI) или «не знаю»: ``statvfs``
    упал либо ФС сообщает нулевой размер. Ошибку watchdog не поднимает — он не
    имеет права падать из-за того, о чём должен доложить.
    """
    if not path:
        return None
    try:
        stats = os.statvfs(path)
    except OSError:
        return None
    total_bytes = stats.f_blocks * stats.f_frsize
    if total_bytes <= 0:
        return None
    return DiskProbe(path=path, free_bytes=stats.f_bavail * stats.f_frsize, total_bytes=total_bytes)


def is_disk_low(disk: DiskProbe, *, min_free_ratio: float) -> bool:
    """Вход в алерт: свободного меньше порога."""
    return disk.free_ratio < min_free_ratio


def is_disk_recovered(disk: DiskProbe, *, min_free_ratio: float) -> bool:
    """Выход из алерта: свободного не меньше порога плюс запас."""
    return disk.free_ratio >= min_free_ratio + _RECOVERY_MARGIN


def describe_disk(disk: DiskProbe, *, min_free_ratio: float) -> str:
    """«свободно 4,8 ГБ из 38 ГБ (13%), порог 15%» — общая часть обоих текстов."""
    free = f"{disk.free_bytes / _GIB:.1f}".replace(".", ",")
    total = round(disk.total_bytes / _GIB)
    # Вниз и целочисленно: 14,9% не выглядят «15%, порог 15%», а 29% — «28%».
    percent = disk.free_bytes * 100 // disk.total_bytes
    return f"свободно {free} ГБ из {total} ГБ ({percent}%), порог {min_free_ratio * 100:g}%"
