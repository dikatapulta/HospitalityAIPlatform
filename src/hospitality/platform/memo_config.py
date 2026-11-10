"""Служебные записки: настройки отеля (spec 0077 §6–§8, #936).

Данные тенанта (P-11, NG-7): шаблоны записок, срок хранения, праздники для срока
объяснения. Поля `TenantConfig.memo_templates`, `memo_retention_days`, `holidays`
и флаг `memos_enabled`; отдельный файл, а не ещё один класс в `config.py`: тот за
границей R-3 (как `showroom_config.py`). Онбординг все четыре переносит из прежнего
конфига (`tools/onboard_tenant.build_config`, класс #177): шаблоны правит менеджер,
праздники вписывают на год — профиль отеля их не несёт.
"""

from __future__ import annotations

from datetime import date
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

MAX_MEMO_TEMPLATES: Final = 10
DEFAULT_MEMO_RETENTION_DAYS: Final = 1095
MIN_MEMO_RETENTION_DAYS: Final = 365
MAX_MEMO_RETENTION_DAYS: Final = 3650
# Праздников на несколько лет вперёд хватит с запасом; больше — опечатка в файле.
MAX_HOLIDAYS: Final = 120


class MemoTemplate(BaseModel):
    """Шаблон записки (§7): `body` подставляется в текст черновика."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    title: str = Field(min_length=1, max_length=80)
    body: str = Field(min_length=1, max_length=2000)


# Набор по умолчанию — слово основателя 10.11 «пока просто делай, потом сам подкорректирую».
DEFAULT_MEMO_TEMPLATES: Final[tuple[MemoTemplate, ...]] = (
    MemoTemplate(
        key="purchase",
        title="О закупке",
        body=(
            "Прошу закупить: (что и сколько).\nДля чего: …\nК какому сроку: …\n"
            "Примерная стоимость: …"
        ),
    ),
    MemoTemplate(
        key="malfunction",
        title="О неисправности",
        body=(
            "Где: (номер, помещение).\nЧто неисправно и с какого времени: …\n"
            "Чем мешает гостям или работе: …\nПрошу: …"
        ),
    ),
    MemoTemplate(
        key="shift_swap",
        title="О замене смены",
        body="Кто и какая смена (дата): …\nКто заменяет: …\nПричина: …",
    ),
)


def ensure_valid_memo_templates(value: tuple[MemoTemplate, ...]) -> tuple[MemoTemplate, ...]:
    if len(value) > MAX_MEMO_TEMPLATES:
        raise ValueError(f"memo templates limit is {MAX_MEMO_TEMPLATES}, got {len(value)}")
    keys = [template.key for template in value]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate memo template key")
    return value


def ensure_valid_holidays(value: tuple[date, ...]) -> tuple[date, ...]:
    if len(value) > MAX_HOLIDAYS:
        raise ValueError(f"holidays limit is {MAX_HOLIDAYS}, got {len(value)}")
    return tuple(sorted(set(value)))
