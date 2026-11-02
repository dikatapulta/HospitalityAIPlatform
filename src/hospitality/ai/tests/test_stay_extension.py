"""Блок «Staying longer» (spec 0059 §2.4, §5 п. 4) и обвязка его evals (§5 п. 5).

Текст блока — по готовому outlook, без БД. Блок в системном промпте с настоящим
outlook по Fake-адаптеру — `tests/test_stay_extension_block.py`: адаптеры из
`ai/` не импортируются (контракт 2 import-linter).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from hospitality.ai import orchestrator
from hospitality.ai.evals import stay_extension as evals
from hospitality.ai.evals.bakeoff import _eval_tenant_id
from hospitality.ai.gateway.api import MockTurn, ScriptedLlmProvider
from hospitality.ai.stay_extension import render_stay_extension_block, stay_extension_block
from hospitality.modules.guests.api import StayExtensionOutlook
from hospitality.shared.ports.pms import pms_or_none, set_pms
from hospitality.shared.tenancy import tenant_context

ALMATY = ZoneInfo("Asia/Almaty")


def _outlook(**changes: object) -> StayExtensionOutlook:
    values: dict[str, object] = {
        "today": date(2026, 11, 2),
        "departure": date(2026, 11, 3),
        "checkout_hour": 12,
        # 09:05 UTC — 14:05 в Алматы (UTC+5).
        "updated_at": datetime(2026, 11, 2, 9, 5, tzinfo=UTC),
        "known_through": date(2026, 12, 1),
        "full_nights": (date(2026, 11, 4),),
    }
    values.update(changes)
    return StayExtensionOutlook.model_validate(values)


def test_block_with_full_nights_reads_dates_in_hotel_zone() -> None:
    block = render_stay_extension_block(_outlook(), ALMATY)

    assert block.startswith("\n\n# Staying longer\n\n")
    assert (
        "Today at the hotel is Monday, 2 November 2026. This guest's checkout is "
        "Tuesday, 3 November 2026 at 12:00." in block
    )
    assert "up to the night of 1–2 December 2026 (updated today at 14:05)." in block
    assert "Nights with NO free rooms: 4–5 November." in block
    assert "You know nothing about nights after 1–2 December," in block
    assert "Every one of these nights has free rooms." not in block


def test_block_without_full_nights_says_every_night_is_free() -> None:
    block = render_stay_extension_block(_outlook(full_nights=()), ALMATY)

    assert "Every one of these nights has free rooms." in block
    assert "Nights with NO free rooms" not in block


def test_full_nights_listed_one_by_one_across_months() -> None:
    nights = (date(2026, 11, 29), date(2026, 11, 30), date(2026, 12, 1))

    block = render_stay_extension_block(_outlook(full_nights=nights), ALMATY)

    assert (
        "Nights with NO free rooms: 29–30 November, 30 November–1 December, 1–2 December." in block
    )


def test_file_from_yesterday_and_night_across_years() -> None:
    """Файл принят до полуночи по поясу отеля — «вчера» датой; ночь 31.12 — с двумя годами."""
    block = render_stay_extension_block(
        _outlook(
            today=date(2026, 12, 2),
            departure=date(2026, 12, 4),
            # 18:50 UTC 1 декабря — 23:50 в Алматы, накануне «сегодня».
            updated_at=datetime(2026, 12, 1, 18, 50, tzinfo=UTC),
            known_through=date(2026, 12, 31),
            full_nights=(),
        ),
        ALMATY,
    )

    assert "(updated on 1 December at 23:50)" in block
    assert "up to the night of 31 December 2026–1 January 2027" in block


def test_rules_forbid_promise_price_and_unasked_mention() -> None:
    """Формулировка, которая обещает продление, — вопрос основателю (§2.4): стережём её."""
    block = render_stay_extension_block(_outlook(), ALMATY)

    assert 'as "very likely", never as certain' in block
    assert "Never say that the stay is extended, and never name a price." in block
    assert "if the guest has not asked to stay longer, say nothing" in block


def test_empty_outlook_and_missing_turn_data_mean_no_block() -> None:
    assert render_stay_extension_block(None, ALMATY) == ""


async def test_no_stay_or_no_config_means_no_block() -> None:
    """Без привязки к Stay и без конфига хода outlook даже не читается."""
    assert await stay_extension_block(None, None) == ""
    assert await stay_extension_block(uuid.uuid4(), None) == ""


async def test_extension_evals_plumbing_on_fake_provider(canonical_database: None) -> None:
    """Обвязка платных evals проверяется бесплатно: тенант с Opera, свежий Stay при
    повторном запуске, блок с занятой ночью в промпте и оценка хода."""
    await evals._ensure_extension_tenant()
    stay_id, today, departure = await evals._ensure_extension_tenant()
    full = next(s for s in evals.SCENARIOS if (s.language, s.kind) == ("tr", "full"))
    unasked = next(s for s in evals.SCENARIOS if (s.language, s.kind) == ("en", "unasked"))
    port = evals._FixedAvailability()
    port.availability = evals._availability("full", today, departure)
    previous = pms_or_none()
    set_pms(port)
    provider = ScriptedLlmProvider(
        [
            MockTurn(text="Muhtemelen 1 gece müsait değil; resepsiyon onaylayacak."),
            MockTurn(text="Checkout is at 12:00. We also have free rooms tonight."),
        ]
    )
    try:
        with tenant_context(await _eval_tenant_id()):
            turns = [
                await orchestrator.handle_message(
                    message=scenario.message,
                    provider=provider,
                    verified_room_number=evals._ROOM,
                    verified_stay_id=stay_id,
                )
                for scenario in (full, unasked)
            ]
    finally:
        if previous is not None:
            set_pms(previous)

    system = provider.calls[0].system or ""
    full_night = departure + timedelta(days=1)
    assert f"Nights with NO free rooms: {full_night.day}–" in system
    assert len(evals.SCENARIOS) == 30
    assert evals._check(full, turns[0])[0] is True
    assert evals._check(unasked, turns[1])[0] is False
    priced = orchestrator.OrchestratorTurn(
        kind=orchestrator.TurnKind.REPLY, reply_text="Ek gece 25 000 ₸."
    )
    assert evals._check(full, priced)[0] is False
