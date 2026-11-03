"""Сводка сцены для промпта помощника (spec 0069 §2.3 п. 2)."""

from __future__ import annotations

from datetime import date

from hospitality.ai.evals.showroom_navigate import EVAL_SCENE
from hospitality.ai.showroom.summary import render_summary
from hospitality.platform.config_values import LocalizedText


def test_summary_names_today_the_visitor_and_floors_by_purpose() -> None:
    text = render_summary(EVAL_SCENE, audience="booking", today=date(2027, 3, 1))

    assert "Today at the hotel is Monday, 1 March 2027." in text
    assert "a private guest choosing dates and a room" in text
    assert "- 3–20: rooms — north 8, south 8 rooms; types: deluxe, standard" in text
    assert '- 2: restaurant · hall "Alatau hall" (banquet 200, theatre 300)' in text
    assert "- 21: monthly — north 1 rooms; types: deluxe (not for sale)" in text
    assert "1501" not in text  # номеров в сводке нет: поиск делает код


def test_summary_never_carries_the_hotel_price_note() -> None:
    """Цены модель не видит — и назвать не может (§2.2: ориентир только на странице)."""
    priced = EVAL_SCENE.model_copy(
        update={
            "room_types": {
                key: info.model_copy(
                    update={"price_note": LocalizedText(en="from 45,000 KZT a night")}
                )
                for key, info in EVAL_SCENE.room_types.items()
            }
        }
    )

    text = render_summary(priced, audience="group", today=date(2026, 11, 4))

    assert "45,000" not in text
    assert "KZT" not in text
