"""CLI заселения (spec 0027 §1.6, §5 тест 7): единственный путь выдачи кода
до кабинета персонала (#48), поэтому покрыт: заселение печатает код один раз,
перевыпуск, выезд, отказ по несуществующей комнате — ненулевой код возврата.

Разбор аргументов и вывод (`main`) — на заглушке `_run` (как у канона
`tests/test_staff_routing_tool.py`); работа с БД — через `_run` на живом
демо-тенанте.
"""

from __future__ import annotations

import argparse

import pytest

from hospitality.modules.guests.api import find_active_stay
from hospitality.platform.seed import DEMO_TENANT_SLUG, seed_demo_tenant
from hospitality.shared.tenancy import tenant_context
from hospitality.tools.checkin import CheckinError, _parse_check_out, _run, main


def _args(**overrides: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "room": None,
        "guest": None,
        "nights": 1,
        "check_out": None,
        "reissue": False,
        "check_out_now": False,
        "list": False,
        "tenant_slug": DEMO_TENANT_SLUG,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


async def test_checkin_prints_code_and_reissue_rotates(canonical_database: None) -> None:
    tenant_id = await seed_demo_tenant()

    lines = await _run(_args(room="101", guest="Wang Li"))
    assert any("КОД ЗАСЕЛЕНИЯ" in line for line in lines)
    with tenant_context(tenant_id):
        stay = await find_active_stay("101")
    assert stay is not None

    reissue_lines = await _run(_args(room="101", reissue=True))
    assert any("Новый код" in line for line in reissue_lines)

    listing = await _run(_args(list=True))
    assert any("101" in line for line in listing)

    checkout_lines = await _run(_args(room="101", check_out_now=True))
    assert any("выезд оформлен" in line for line in checkout_lines)
    with tenant_context(tenant_id):
        assert await find_active_stay("101") is None


async def test_reissue_unknown_room_fails(canonical_database: None) -> None:
    await seed_demo_tenant()
    with pytest.raises(CheckinError, match="Активного проживания"):
        await _run(_args(room="404", reissue=True))


async def test_unknown_tenant_fails(canonical_database: None) -> None:
    with pytest.raises(CheckinError, match="не найден"):
        await _run(_args(room="101", tenant_slug="no-such-hotel"))


def test_parse_check_out_validates_input() -> None:
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Asia/Almaty")
    now = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)

    parsed = _parse_check_out("2026-07-29 12:00", 1, zone, now=now)
    assert parsed == datetime(2026, 7, 29, 12, 0, tzinfo=zone).astimezone(UTC)

    default = _parse_check_out(None, 2, zone, now=now)
    assert default.astimezone(zone).hour == 12

    with pytest.raises(CheckinError, match="формат"):
        _parse_check_out("29.07.2026", 1, zone, now=now)
    with pytest.raises(CheckinError, match="в прошлом"):
        _parse_check_out("2026-07-01 12:00", 1, zone, now=now)


def test_main_requires_room_or_list(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "номер комнаты" in capsys.readouterr().err


def test_main_rejects_conflicting_flags(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["101", "--reissue", "--check-out-now"]) == 2
    assert "нельзя совмещать" in capsys.readouterr().err
