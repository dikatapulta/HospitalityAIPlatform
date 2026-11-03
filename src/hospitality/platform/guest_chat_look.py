"""Вид гостевого чата, выбранный отелем (spec 0066 §3, #761): цвет подложки, узор, знак.

Поле `TenantConfig.guest_chat_look`; отдельный файл, а не ещё один класс в
`config.py`: тот за границей R-3 (как `table_booking_config.py`). Здесь — только
ключи выбора; цвета вариантов и множители узора — у веб-канала
(`channels/web/look.py`): ядро не знает, как рисуется страница гостя (R-5).

Источник — страница кабинета «Вид гостевого чата», не файл профиля: онбординг
переносит прежнее значение (`tools/onboard_tenant.build_config`).
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict

# Готовые варианты подложки (§2 п. 1); первый — умолчание #746.
GuestChatPalette = Literal["sky_gold", "emerald", "sunset"]
# Заметность узора (§2 п. 2): множители — `channels/web/look.py`.
GuestChatPattern = Literal["off", "quiet", "normal", "bright"]


class GuestChatLook(BaseModel):
    """Выбор отеля; умолчание — вид #746: «Небо и золото», обычный узор, буква в кружке."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    palette: GuestChatPalette = "sky_gold"
    pattern: GuestChatPattern = "normal"
    # Строка `uploaded_files` назначения `hotel_logo` (spec 0047); `None` — знака нет.
    logo_file_id: uuid.UUID | None = None

    @property
    def is_default(self) -> bool:
        """Ни варианта, ни узора, ни знака — страница гостя байт в байт как у #746."""
        return self == GuestChatLook()
