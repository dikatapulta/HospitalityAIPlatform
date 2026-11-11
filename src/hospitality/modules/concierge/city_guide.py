"""Справочник города: файл данных, его проверка и выбор по городу отеля (spec 0085 §3).

Справочник — общие знания (FOUNDATION §7.4): записи не принадлежат отелю, поэтому
живут файлом в репозитории (`data/<город>.toml`), а не в БД тенанта. Правка — PR,
ревью и CI, как у кода: у каждой записи источник и дата проверки, и тест модуля
не пропустит файл без них.

Город выбирается по профилю отеля (`HotelProfile.city` и `country_code`), а не
вторым полем конфига: город, записанный дважды, однажды разойдётся, и отель
в Астане получил бы справочник Алматы — неправда гостю. Профиль, для которого
справочника нет, — `None`, блока в промпте нет.
"""

from __future__ import annotations

import tomllib
from datetime import date
from functools import cache
from pathlib import Path
from typing import Final
from urllib.parse import quote_plus

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

_DATA_DIR: Final = Path(__file__).parent / "data"

# Пределы — в схеме, а не в тесте содержимого: справочник целиком едет в
# системный промпт каждого хода, и его длина — это деньги (spec 0085 §4).
MAX_CITY_GUIDE_ENTRIES: Final = 60
MAX_CITY_GUIDE_TOPIC_LENGTH: Final = 40
MAX_CITY_GUIDE_TEXT_LENGTH: Final = 300
MAX_CITY_GUIDE_TOTAL_TEXT_CHARS: Final = 12_000

# Ссылка поиска на карте без ключа и без платного API — Google Maps URLs
# (https://developers.google.com/maps/documentation/urls/get-started, сверено
# 11.11.2026). Порт карт — когда появится ключ провайдера (spec 0085 §7).
MAP_SEARCH_URL: Final = "https://www.google.com/maps/search/?api=1&query="

_LINE_BREAKS: Final = frozenset("\n\r\v\f\x1c\x1d\x1e\x85  ")


class CityGuideEntry(BaseModel):
    """Одна запись справочника: тема, ответ гостю, источник и дата проверки."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    topic: str = Field(min_length=1, max_length=MAX_CITY_GUIDE_TOPIC_LENGTH)
    text: str = Field(min_length=1, max_length=MAX_CITY_GUIDE_TEXT_LENGTH)
    # Местное название — показать таксисту; только у мест.
    local_name: str | None = Field(default=None, min_length=1, max_length=60)
    # Запрос поиска на карте латиницей; город к нему добавляет `CityGuide.map_url`.
    map: str | None = Field(default=None, min_length=1, max_length=60)
    source: str = Field(pattern=r"^https://\S+$")
    checked: date

    @field_validator("topic", "text", "local_name", "map")
    @classmethod
    def _one_line(cls, value: str | None) -> str | None:
        # Запись — ровно одна строка блока промпта: вторая строка встала бы туда
        # самостоятельным текстом (тот же довод, что у фактов отеля, spec 0036 §3).
        # Данные наши, а не менеджера, — поэтому отказ, а не тихая починка.
        if value is not None and (not value.strip() or _LINE_BREAKS & set(value)):
            raise ValueError(f"city guide value must be one non-empty line: {value!r}")
        return value

    @field_validator("topic")
    @classmethod
    def _topic_is_a_heading(cls, value: str) -> str:
        if "/" in value:
            raise ValueError(f"city guide topic must not contain '/': {value!r}")
        return value


class CityGuide(BaseModel):
    """Справочник одного города. `key` — имя файла данных без расширения."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    # Имя города в заголовке блока и в запросе карты.
    name: str = Field(min_length=1, max_length=60)
    country_code: str = Field(pattern=r"^[A-Z]{2}$")
    # Как город может быть записан в профиле отеля: «Almaty», «Алматы», «Алма-Ата».
    aliases: tuple[str, ...] = Field(min_length=1)
    entries: tuple[CityGuideEntry, ...] = Field(min_length=1, max_length=MAX_CITY_GUIDE_ENTRIES)

    @model_validator(mode="after")
    def _topics_unique_and_total_bounded(self) -> CityGuide:
        topics = [entry.topic.strip().casefold() for entry in self.entries]
        duplicates = sorted({topic for topic in topics if topics.count(topic) > 1})
        if duplicates:
            raise ValueError(f"duplicate city guide topics: {duplicates}")
        total = sum(len(entry.text) for entry in self.entries)
        if total > MAX_CITY_GUIDE_TOTAL_TEXT_CHARS:
            raise ValueError(
                f"city guide texts total {total} chars > {MAX_CITY_GUIDE_TOTAL_TEXT_CHARS}"
            )
        return self

    def map_url(self, entry: CityGuideEntry) -> str | None:
        """Ссылка поиска места на карте; у записи без `map` — `None`.

        Ссылку строит код, а не модель: модель её только переписывает, и
        выдуманного или искажённого адреса гость не получит.
        """
        if entry.map is None:
            return None
        return MAP_SEARCH_URL + quote_plus(f"{entry.map} {self.name}")


@cache
def all_city_guides() -> tuple[CityGuide, ...]:
    """Все справочники из `data/`, проверенные схемой; читается один раз на процесс.

    Битый файл — `ValidationError` при первом обращении. До `main` он не доедет:
    тест модуля загружает каждый файл (spec 0085 §8).
    """
    guides = []
    for path in sorted(_DATA_DIR.glob("*.toml")):
        with path.open("rb") as file:
            raw = tomllib.load(file)
        guides.append(CityGuide.model_validate({**raw, "key": path.stem}))
    logger.info("city_guides_loaded", cities=[guide.key for guide in guides])
    return tuple(guides)


def city_guide_for(city: str, country_code: str) -> CityGuide | None:
    """Справочник города отеля по его профилю; нет такого — `None`.

    Сравнение — без регистра, лишних пробелов и разницы «-»/пробел: «Алма-Ата»
    и «алма ата» — одно имя. Страна обязана совпасть: одноимённый город другой
    страны справочник чужого не получит.
    """
    wanted = _normalized(city)
    for guide in all_city_guides():
        if guide.country_code != country_code:
            continue
        if any(_normalized(alias) == wanted for alias in guide.aliases):
            return guide
    return None


def _normalized(name: str) -> str:
    return " ".join(name.replace("-", " ").split()).casefold()
