"""Кирпичи конфига тенанта, общие для его схем: ключ службы и текст на языках гостя.

Отдельный файл, чтобы части конфига в своих файлах (`service_quotas.py`,
spec 0056 §3) брали их, не импортируя `config.py`: тот сам импортирует эти
части в `TenantConfig`, и обратный импорт был бы круговым. Снаружи их по-прежнему
берут из `config.py` — он реэкспортирует.
"""

from __future__ import annotations

import re
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

# Формат ключа категории заявок — копия паттерна `RequestCategoryCreate.key`
# (модуль requests). Дублируется намеренно: kernel не импортирует доменные
# модули (R-5), а опечатка в ключе обязана падать здесь, а не молча выключать
# маршрутизацию уведомлений (spec 0026).
CATEGORY_KEY_PATTERN: Final = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# Языки текстов, которые отель пишет сам на трёх языках (spec 0038 Р-6):
# `en` обязателен — его видит гость, для чьего языка перевода нет.
LOCALIZED_TEXT_LANGUAGES: Final = ("en", "ru", "kk")
# Предел одного перевода — длина описания товара на странице кабинета
# (прототип панели spec 0038); названия режет модуль строже.
MAX_LOCALIZED_TEXT_LENGTH: Final = 300


class LocalizedText(BaseModel):
    """Текст, который отель пишет сам, на языках гостя (spec 0038 Р-6, GLOSSARY).

    Названия и описания каталога room service, заметка о доставке каталога.
    В БД — JSONB-объект `{"en": …, "ru": …, "kk": …}` (`as_json`). `en`
    обязателен: его видит гость, для чьего языка перевода нет (`text_for`).
    Ключ вне `LOCALIZED_TEXT_LANGUAGES` — ошибка (`extra="forbid"`): перевод на
    язык, который платформа не показывает, пропадал бы молча. Пробельный
    перевод — это «перевода нет», а не пустая строка на странице гостя.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    en: str = Field(max_length=MAX_LOCALIZED_TEXT_LENGTH)
    ru: str | None = Field(default=None, max_length=MAX_LOCALIZED_TEXT_LENGTH)
    kk: str | None = Field(default=None, max_length=MAX_LOCALIZED_TEXT_LENGTH)

    @field_validator("en", "ru", "kk", mode="before")
    @classmethod
    def _blank_means_no_translation(cls, value: object, info: ValidationInfo) -> object:
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if stripped:
            return stripped
        if info.field_name == "en":
            raise ValueError("English text is required: guests without a translation see it")
        return None

    def text_for(self, language: str) -> str:
        """Текст на языке гостя; перевода нет (или язык чужой) — английский."""
        translations = {"ru": self.ru, "kk": self.kk}
        return translations.get(language) or self.en

    def as_json(self) -> dict[str, str]:
        """Форма для JSONB-колонки: только заданные переводы."""
        return {
            language: text
            for language, text in (("en", self.en), ("ru", self.ru), ("kk", self.kk))
            if text is not None
        }
