"""Фото гостя в карточке заявки (spec 0081 §7, issue #82).

Лента и панель заявки показывают миниатюры фото, которые гость приложил к
заявке из чата; касание открывает фото в новой вкладке. Данные — одним запросом
на страницу ленты (`requests_api.list_request_photo_ids`). Отдельный файл, а не
код в `queue.py`: тот за границей R-3.

Выдача файла — маршрут `requests_pages.request_photo` (здесь его нет: `pages`
отсюда дал бы циклический импорт через `queue.py`), только в границе служб
сотрудника (spec 0045 §5), той же, что у ленты: фото заявки чужой службы — 404,
как несуществующее (`ERR-REQUESTS-010`).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import Final

from hospitality.modules.requests import api as requests_api

PHOTO_ALT: Final = "Фото гостя"


def photo_path(tenant_slug: str, request_id: uuid.UUID, file_id: uuid.UUID) -> str:
    """Адрес фото заявки в кабинете — он же `src` миниатюры."""
    return f"/staff/{tenant_slug}/requests/{request_id}/photos/{file_id}"


async def photo_cards(
    tenant_slug: str, request_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, list[dict[str, str]]]:
    """Миниатюры к карточкам ленты: заявка → [{href, alt}]; заявки без фото — нет ключа."""
    photos = await requests_api.list_request_photo_ids(request_ids)
    return {
        request_id: [
            {"href": photo_path(tenant_slug, request_id, file_id), "alt": f"{PHOTO_ALT} {number}"}
            for number, file_id in enumerate(file_ids, start=1)
        ]
        for request_id, file_ids in photos.items()
    }
