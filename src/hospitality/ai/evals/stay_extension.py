"""Evals блока «Staying longer» на языках пилота (spec 0059 §5 п. 5, FOUNDATION §7.7).

Что мерим — что слышит гость, когда бот видит свободные номера по ночам из Opera.
Выезд гостя — послезавтра в 12:00; файл — 30 ночей с сегодняшней:
- `free` — две ночи сверх выезда, места есть: «скорее всего» и ресепшен;
- `full` — три ночи, вторая из них без мест: «скорее всего, не получится»;
- `beyond` — на два месяца, дальше файла: уточнит ресепшен;
- `late` — выезд в 16:00 в последний день: вывода из блока нет, ресепшен;
- `unasked` — «во сколько выезд?» при той же занятой ночи: о свободных и занятых
  ночах бот молчит (находка ревью PR #620 в `docs/later/`).

Машинно — язык (кириллица у гостя en/zh/tr/hi), цена (знак или название валюты)
и у `unasked` слова «свободные номера» на языке гостя (кроме kk: «бос» живёт и в
«босату» — освободить номер). «Скорее всего», «не продлено» и ресепшен оценивает
человек по печати. Режим — «только консультации», как на гостевой копии: у
eval-тенанта нет службы reception, и ход с заявкой проверял бы не ту категорию.

Файл Opera подставляет порт-заглушка `_FixedAvailability`: адаптеры из `ai/`
не импортируются (контракт 2 import-linter). Конфиг eval-тенанта получает
`pms_source: opera_report` — следующий прогон bakeoff его перезапишет. Запуск
ПЛАТНЫЙ (30 ходов модели гостя), только с согласия основателя (CLAUDE.md);
Postgres и ключ — как у bakeoff:

    python -m hospitality.ai.evals.stay_extension [--repeat N] [модель ...]
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

from hospitality.ai import orchestrator
from hospitality.ai.evals.bakeoff import _build_candidates, _ensure_eval_tenant, _eval_tenant_id
from hospitality.modules.guests import api as guests_api
from hospitality.platform.config import load_tenant_config, store_tenant_config
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.ports.pms import (
    PmsAvailability,
    PmsNightAvailability,
    PmsSnapshot,
    set_pms,
)
from hospitality.shared.tenancy import tenant_context

Kind = Literal["free", "full", "beyond", "late", "unasked"]
_KINDS: tuple[Kind, ...] = ("free", "full", "beyond", "late", "unasked")


@dataclass(frozen=True)
class Scenario:
    language: str
    kind: Kind
    message: str


_MESSAGES: dict[str, tuple[str, str, str, str, str]] = {
    "en": (
        "Can I stay two more nights?",
        "Could I extend my stay by three nights?",
        "Can I extend my stay by two months?",
        "Can I check out at 4 pm on my last day?",
        "What time do I have to check out?",
    ),
    "ru": (
        "Можно остаться ещё на две ночи?",
        "Можно продлить проживание на три ночи?",
        "Можно продлить проживание на два месяца?",
        "Можно в последний день выехать в 16:00?",
        "Во сколько мне нужно выехать?",
    ),
    "kk": (
        "Тағы екі түнге қалуға бола ма?",
        "Тұруымды үш түнге ұзартуға бола ма?",
        "Тұруымды екі айға ұзартуға бола ма?",
        "Соңғы күні сағат 16:00-де шығуға бола ма?",
        "Қонақүйден сағат нешеде шығуым керек?",
    ),
    "zh": (
        "我可以再住两晚吗？",
        "我可以把住宿延长三晚吗？",
        "我可以把住宿延长两个月吗？",
        "最后一天我可以下午4点退房吗？",
        "我几点必须退房？",
    ),
    "tr": (
        "İki gece daha kalabilir miyim?",
        "Konaklamamı üç gece uzatabilir miyim?",
        "Konaklamamı iki ay uzatabilir miyim?",
        "Son gün saat 16:00'da çıkış yapabilir miyim?",
        "Saat kaçta çıkış yapmam gerekiyor?",
    ),
    "hi": (
        "क्या मैं दो रात और रुक सकता हूँ?",
        "क्या मैं अपना ठहराव तीन रात बढ़ा सकता हूँ?",
        "क्या मैं अपना ठहराव दो महीने बढ़ा सकता हूँ?",
        "क्या मैं आख़िरी दिन दोपहर 4 बजे चेक-आउट कर सकता हूँ?",
        "मुझे कितने बजे चेक-आउट करना है?",
    ),
}

SCENARIOS: list[Scenario] = [
    Scenario(language, kind, message)
    for language, messages in _MESSAGES.items()
    for kind, message in zip(_KINDS, messages, strict=True)
]

_ROOM = "eval-extend"
_DEPARTURE_IN_DAYS = 2
_FILE_NIGHTS = 30
_FREE_ROOMS_PER_NIGHT = 12
# Вторая ночь сверх выезда — без мест: у `full` она внутри просьбы, у `unasked` —
# соблазн заговорить о ней без вопроса.
_FULL_NIGHT_KINDS: tuple[Kind, ...] = ("full", "unasked")

# Гость этих языков кириллицу читать не обязан: она в ответе — чужой язык (#342).
_NON_CYRILLIC = ("en", "zh", "tr", "hi")
_PRICE_MARKS = ("₸", "KZT", "тенге", "теңге", "tenge", "$", "€")
_FREE_ROOM_WORDS: dict[str, tuple[str, ...]] = {
    "en": ("free room", "rooms available", "available room", "vacan"),
    "ru": ("свободн",),
    "zh": ("空房", "有房"),
    "tr": ("boş oda", "müsait"),
    "hi": ("खाली",),
}


class _FixedAvailability:
    """Порт PMS на прогон: файл свободных номеров кладёт сценарий, снимков нет."""

    backend = "eval"

    def __init__(self) -> None:
        self.availability: PmsAvailability | None = None

    async def fetch_snapshot(self, tenant_id: uuid.UUID) -> PmsSnapshot | None:
        return None

    async def fetch_availability(self, tenant_id: uuid.UUID) -> PmsAvailability | None:
        return self.availability

    async def check(self) -> bool:
        return True


def _availability(kind: Kind, today: date, departure: date) -> PmsAvailability:
    full_night = departure + timedelta(days=1)
    nights: list[PmsNightAvailability] = []
    for offset in range(_FILE_NIGHTS):
        night = today + timedelta(days=offset)
        busy = kind in _FULL_NIGHT_KINDS and night == full_night
        nights.append(PmsNightAvailability(night, 0 if busy else _FREE_ROOMS_PER_NIGHT))
    return PmsAvailability(taken_at=utc_now(), nights=tuple(nights))


async def _ensure_extension_tenant() -> tuple[uuid.UUID, date, date]:
    """Eval-тенант bakeoff с выгрузкой Opera и свежий Stay до послезавтра 12:00.

    Вернуть id Stay, «сегодня» и дату выезда по поясу отеля. Прежний Stay комнаты
    выписывается: его выезд считался от дня прошлого прогона.
    """
    await _ensure_eval_tenant()
    tenant_id = await _eval_tenant_id()
    async with platform_session_scope() as session:
        config = await load_tenant_config(session, tenant_id)
        await store_tenant_config(
            session, tenant_id, config.model_copy(update={"pms_source": "opera_report"})
        )
    zone = config.tzinfo
    today = utc_now().astimezone(zone).date()
    departure = today + timedelta(days=_DEPARTURE_IN_DAYS)
    with tenant_context(tenant_id):
        previous = await guests_api.find_active_stay(_ROOM)
        if previous is not None:
            await guests_api.check_out(previous.id)
        checked = await guests_api.check_in(
            guests_api.StayCheckIn(
                room_number=_ROOM,
                check_out_at=datetime.combine(departure, time(12), tzinfo=zone).astimezone(UTC),
            )
        )
    return checked.stay.id, today, departure


def _check(scenario: Scenario, turn: orchestrator.OrchestratorTurn) -> tuple[bool, str]:
    reply = turn.reply_text
    if turn.kind is orchestrator.TurnKind.NEEDS_HUMAN:
        return False, f"эскалация вместо ответа: {reply[:90]!r}"
    if scenario.language in _NON_CYRILLIC and any("а" <= c.lower() <= "я" for c in reply):
        return False, f"кириллица у гостя {scenario.language}: {reply[:90]!r}"
    if any(mark.lower() in reply.lower() for mark in _PRICE_MARKS):
        return False, f"названа цена: {reply[:90]!r}"
    words = _FREE_ROOM_WORDS.get(scenario.language, ())
    if scenario.kind == "unasked" and any(word in reply.lower() for word in words):
        return False, f"о свободных номерах без вопроса: {reply[:90]!r}"
    return True, reply


async def run(models: list[str], repeat: int) -> None:
    providers = _build_candidates(models)
    if not providers:
        print("Ни один кандидат не собран — причины выше.")
        return
    os.environ["ENABLE_SERVICE_REQUESTS"] = "false"
    get_settings.cache_clear()
    stay_id, today, departure = await _ensure_extension_tenant()
    tenant_id = await _eval_tenant_id()
    port = _FixedAvailability()
    set_pms(port)
    failures = 0
    for model, provider in providers.items():
        print(f"\n===== {model}, повторов: {repeat}; выезд {departure}, сегодня {today} =====")
        for scenario in SCENARIOS:
            for _ in range(repeat):
                port.availability = _availability(scenario.kind, today, departure)
                try:
                    with tenant_context(tenant_id):
                        turn = await orchestrator.handle_message(
                            message=scenario.message,
                            provider=provider,
                            verified_room_number=_ROOM,
                            verified_stay_id=stay_id,
                        )
                    ok, detail = _check(scenario, turn)
                except AppError as error:
                    ok, detail = False, f"ERROR {error.code}: {error.message}"
                failures += not ok
                print(f"  {'OK ' if ok else '!! '}[{scenario.language}/{scenario.kind}] {detail}")
    print(f"\nПровалов: {failures}. Ответы — прочесть глазами (spec 0059 §5 п. 5).")
    if failures:
        sys.exit(1)


def _parse(argv: list[str]) -> tuple[list[str], int]:
    repeat = 1
    if "--repeat" in argv:
        index = argv.index("--repeat")
        repeat = int(argv[index + 1])
        argv = argv[:index] + argv[index + 2 :]
    return argv or [get_settings().llm_model], repeat


if __name__ == "__main__":
    asyncio.run(run(*_parse(sys.argv[1:])))
