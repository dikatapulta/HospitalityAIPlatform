"""3D-витрина: настройки отеля (spec 0069 §2.5, ADR-025).

Данные тенанта (P-11, NG-7): включена ли витрина, по коду ли она или публична,
какие службы получают запросы посетителей. Поле `TenantConfig.showroom`; отдельный
файл, а не ещё один класс в `config.py`: тот за границей R-3 (как
`table_booking_config.py`).

Код доступа ставит только команда `tools/showroom_access_code` (в базе — bcrypt-хэш,
сам код нигде не хранится); онбординг хэш и версию кода переносит из прежнего конфига
и задать их файлом профиля не даёт. Что службы — категории заявок отеля, сверяет
онбординг (`tools/onboard_tenant`): категории живут в БД, а не в конфиге.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from hospitality.platform.config_values import CATEGORY_KEY_PATTERN, LocalizedText

# Гостей в запросе на бронирование — не больше (§2.5): больше — уже группа.
MAX_BOOKING_GUESTS_LIMIT: Final = 30


class ShowroomConfig(BaseModel):
    """Витрина отеля (§2.5). Три режима (§2.2): кабинет — `enabled`; по коду —
    ещё и `access_code_hash`; публично — `public_enabled` (только после
    допсоглашения и письменного «да» отеля, §2.6). Без сцены витрины нет нигде."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    # bcrypt-хэш кода доступа; None — страницы по коду нет.
    access_code_hash: str | None = Field(default=None, max_length=128)
    # +1 при каждой смене кода: подпись cookie строится над версией, прежние гаснут.
    access_code_version: int = Field(default=0, ge=0)
    public_enabled: bool = False
    # Служба продаж — групповые запросы (и запросы гостей, если службы бронирования нет).
    sales_category_key: str = Field(pattern=CATEGORY_KEY_PATTERN.pattern, max_length=63)
    booking_category_key: str | None = Field(
        default=None, pattern=CATEGORY_KEY_PATTERN.pattern, max_length=63
    )
    booking_max_guests: int = Field(default=10, ge=1, le=MAX_BOOKING_GUESTS_LIMIT)
    # False — только правила и кнопки, модель не зовётся (§2.3).
    assistant_enabled: bool = True
    # «Отдел продаж отвечает в рабочие часы…» — под формами.
    contact_note: LocalizedText | None = None

    @property
    def category_keys(self) -> frozenset[str]:
        """Службы, которые видят витрину в кабинете и получают её запросы."""
        keys = {self.sales_category_key}
        if self.booking_category_key is not None:
            keys.add(self.booking_category_key)
        return frozenset(keys)
