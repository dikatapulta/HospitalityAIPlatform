"""Справочник города: файл данных, схема и выбор по профилю отеля (spec 0085 §3, §6, §8).

Тесты содержимого — не вкус, а правила основателя для гостевого текста: неправда
гостю — главный риск черновика (#952), и то, что можно проверить машиной, проверяет
машина при каждой правке файла.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from hospitality.modules.concierge import api as concierge_api
from hospitality.shared.db import utc_now


def _entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "topic": "Medeu ice rink",
        "text": "An outdoor ice rink in a mountain gorge above the city.",
        "local_name": "Медеу",
        "map": "Medeu ice rink",
        "source": "https://example.org/medeu",
        "checked": "2026-11-11",
    }
    return {**entry, **overrides}


def _guide(*entries: dict[str, Any]) -> concierge_api.CityGuide:
    return concierge_api.CityGuide.model_validate(
        {
            "key": "testcity",
            "name": "Almaty",
            "country_code": "KZ",
            "aliases": ["Almaty"],
            "entries": list(entries) or [_entry()],
        }
    )


def test_every_data_file_passes_the_schema() -> None:
    """Битый файл не доедет до `main`: схема проверяет каждый файл `data/` (§8)."""
    guides = concierge_api.all_city_guides()
    assert [guide.key for guide in guides] == ["almaty"]


@pytest.mark.parametrize("city", ["Almaty", "almaty", "Алматы", "Алма-Ата", "алма ата", " Almaty "])
def test_almaty_guide_is_found_by_how_a_profile_spells_the_city(city: str) -> None:
    guide = concierge_api.city_guide_for(city, "KZ")
    assert guide is not None and guide.key == "almaty"


@pytest.mark.parametrize(("city", "country"), [("Astana", "KZ"), ("Almaty", "KG"), ("", "KZ")])
def test_no_guide_for_another_city_or_country(city: str, country: str) -> None:
    """Отель другого города справочника Алматы не получает — неправда гостю (§3)."""
    assert concierge_api.city_guide_for(city, country) is None


def test_almaty_content_has_a_source_and_a_past_check_date() -> None:
    """У каждой записи — источник и дата проверки, и дата не из будущего (§6)."""
    today = utc_now().date()
    for entry in concierge_api.all_city_guides()[0].entries:
        assert entry.source.startswith("https://"), entry.topic
        assert date(2026, 1, 1) <= entry.checked <= today, entry.topic


# Цена — число рядом с валютой: «2 000 ₸», «500 tenge», «KZT 300».
_PRICE = re.compile(r"\d[\d\s.,]*\s*(₸|tenge|kzt|тенге|\$|€|usd)|(₸|kzt|\$|€|usd)\s*\d", re.I)
# Экстренные и справочные номера Казахстана: 101–104, 112, 109.
_EMERGENCY = re.compile(r"(?<![\d-])(10[1-49]|112)(?![\d-])")


def test_almaty_content_names_no_prices_and_no_emergency_numbers() -> None:
    """Правила основателя (§6): цен нет — они меняются; экстренных номеров нет —
    гость внутри отеля, внешние службы вызывает ресепшен."""
    for entry in concierge_api.all_city_guides()[0].entries:
        assert not _PRICE.search(entry.text), entry.topic
        assert not _EMERGENCY.search(entry.text), entry.topic


def test_map_url_is_built_by_code_with_the_city_name() -> None:
    guide = _guide(_entry(map="Green Bazaar"), _entry(topic="Tenge", map=None, local_name=None))
    assert guide.map_url(guide.entries[0]) == (
        "https://www.google.com/maps/search/?api=1&query=Green+Bazaar+Almaty"
    )
    assert guide.map_url(guide.entries[1]) is None


@pytest.mark.parametrize(
    "bad",
    [
        {"text": "first line\n- second line"},
        {"topic": "Taxi / metro"},
        {"topic": "   "},
        {"source": "http://example.org/insecure"},
        {"text": "x" * (concierge_api.MAX_CITY_GUIDE_TEXT_LENGTH + 1)},
        {"price": "1000"},
    ],
)
def test_schema_rejects_a_bad_entry(bad: dict[str, Any]) -> None:
    """Запись — одна строка блока промпта; тема — заголовок; источник — https (§3)."""
    with pytest.raises(ValidationError):
        _guide(_entry(**bad))


def test_schema_rejects_duplicate_topics_and_too_many_entries() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        _guide(_entry(), _entry(topic="medeu ICE rink"))
    too_many = [_entry(topic=f"Place {i}") for i in range(concierge_api.MAX_CITY_GUIDE_ENTRIES + 1)]
    with pytest.raises(ValidationError):
        _guide(*too_many)


def test_schema_bounds_the_total_text_that_goes_into_every_turn() -> None:
    """Справочник едет в каждый ход — предел суммы знаков держит цену хода (§4)."""
    long_text = "y" * concierge_api.MAX_CITY_GUIDE_TEXT_LENGTH
    count = concierge_api.MAX_CITY_GUIDE_TOTAL_TEXT_CHARS // len(long_text) + 1
    assert count <= concierge_api.MAX_CITY_GUIDE_ENTRIES
    with pytest.raises(ValidationError, match="total"):
        _guide(*[_entry(topic=f"Place {i}", text=long_text) for i in range(count)])
