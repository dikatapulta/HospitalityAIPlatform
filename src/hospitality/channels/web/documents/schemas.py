"""Схемы ответов формы реквизитов (spec 0049 §5, R-6).

Тело отправки — multipart (`platform/upload_form.py`), схемы у него нет: поля
проверяет `documents_api.ActRequestSubmit`. Наружу — только то, что показывает
страница: даты уже словами в поясе отеля, без id проживания и гостя.
"""

from __future__ import annotations

from pydantic import BaseModel

from hospitality.channels.web.documents.texts import GuestLanguage


class DocumentsData(BaseModel):
    """Шапка формы: номер и даты — из проживания, не со слов гостя (§1)."""

    language: GuestLanguage
    room_number: str
    check_in_label: str
    check_out_label: str
    # Фото читает модель (Р-13) — подпись «Кто увидит» называет и её (#539).
    photos_read_by_ai: bool


class DocumentsSent(BaseModel):
    """Экран «Отправлено»: сколько файлов принято и к какой дате готовят."""

    files_count: int
    check_out_label: str
