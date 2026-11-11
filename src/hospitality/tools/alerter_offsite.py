"""Копия бэкапа вне сервера устарела — линия алертера (issue #1061, ERR-OPS-023).

Все экземпляры данных копии отеля лежат на одном сервере. Копию вне его каждый час
забирает агент launchd на машине основателя (``ops/backup/fetch-agent.sh``), а
``ops/backup/fetch.sh``, убедившись, что дамп расшифровывается ключом основателя,
ставит рядом с дампами на сервере метку ``offsite-copy.stamp`` со временем изменения
этого дампа (``touch -r``). Возраст метки — возраст свежайшей проверенной копии вне
сервера.

Сторожит метку сервер, а не машина основателя: выключенный, потерянный или
оставшийся без ключа ноутбук — ровно тот отказ, о котором надо сказать, и сам о
себе он не скажет.

Метки нет или она старше ``ALERT_OFFSITE_BACKUP_MAX_AGE_HOURS`` — алерт состояния:
одно сообщение на вход и ✅ на выход. 0 — линия выключена (умолчание: staging —
синтетика, копию вне сервера ему держать незачем). Каталог бэкапов не открывается —
«не знаю», линия молчит и состояния не меняет: эту слепоту уже называет ERR-OPS-008.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from hospitality.shared.alerting import format_alert

# Код каталога (docs/runbooks/errors.md, R-8).
ERR_OFFSITE_STALE = "ERR-OPS-023"

# Имя метки — общий факт с ops/backup/fetch.sh, он её и пишет.
OFFSITE_STAMP_NAME = "offsite-copy.stamp"

_HOUR = 3600.0
_DAY = 86400.0


@dataclass(frozen=True)
class OffsiteProbe:
    """Метка копии вне сервера на момент опроса; ``age_seconds=None`` — метки нет."""

    age_seconds: float | None


def probe_offsite_copy(directory: str, *, now: float) -> OffsiteProbe | None:
    """Возраст метки в каталоге бэкапов; None — линия без данных.

    None — каталог не задан (dev, CI) или не открывается («не знаю»). ``now`` —
    стенные секунды: сравниваются со временем изменения файла. Ошибку watchdog не
    поднимает — он не имеет права падать из-за того, о чём должен доложить.
    """
    if not directory:
        return None
    try:
        mtime = os.stat(os.path.join(directory, OFFSITE_STAMP_NAME)).st_mtime
    except FileNotFoundError:
        # Нет метки — алерт; нет самого каталога — «не знаю».
        if not os.path.isdir(directory):
            return None
        return OffsiteProbe(age_seconds=None)
    except OSError:
        return None
    # Часы сервера ушли назад — не «копия из будущего», а ноль.
    return OffsiteProbe(age_seconds=max(0.0, now - mtime))


def _age(seconds: float) -> str:
    """«9 ч» до трёх суток, дальше «5 сут»: порог — 48 ч, а простой бывает неделями."""
    if seconds < 3 * _DAY:
        return f"{round(seconds / _HOUR)} ч"
    return f"{seconds / _DAY:.0f} сут"


@dataclass
class OffsiteCopyLine:
    """Машина линии ERR-OPS-023 — чистая логика, без I/O."""

    max_age_hours: float
    alert_active: bool = False

    def evaluate(
        self, offsite: OffsiteProbe | None, *, environment: str, runbook_url: str
    ) -> list[str]:
        """Сообщение, если метка перешла порог в любую сторону с прошлого опроса."""
        if self.max_age_hours <= 0 or offsite is None:
            return []
        age = offsite.age_seconds
        stale = age is None or age > self.max_age_hours * _HOUR
        if stale == self.alert_active:
            return []
        self.alert_active = stale
        if stale:
            title, detail, emoji = self._stale(offsite)
        else:
            title = "копия бэкапа вне сервера снова свежая"
            detail = f"свежайшей проверенной копии на машине основателя {_age(age or 0.0)}"
            emoji = "✅"
        return [
            format_alert(
                error_code=ERR_OFFSITE_STALE,
                title=title,
                detail=detail,
                environment=environment,
                runbook_url=runbook_url,
                emoji=emoji,
            )
        ]

    def _stale(self, offsite: OffsiteProbe) -> tuple[str, str, str]:
        threshold = f"{self.max_age_hours:g} ч"
        if offsite.age_seconds is None:
            state = (
                f"метки {OFFSITE_STAMP_NAME} в каталоге бэкапов нет — ни один забор "
                "с проверкой ключом до сервера не дошёл"
            )
        else:
            state = (
                f"свежайшей проверенной копии на машине основателя "
                f"{_age(offsite.age_seconds)} (порог {threshold})"
            )
        return (
            "копия бэкапа вне сервера устарела",
            f"{state}. Данные отеля сейчас только на этом сервере: откажет он — "
            "восстанавливать не из чего. Агент забора на машине основателя не дошёл "
            "до сервера или ключ не подошёл — лог ~/necturn-pilot-backups/"
            "fetch-agent.log; пришёл и ERR-OPS-008 — причина в ночном бэкапе",
            "🔴",
        )
