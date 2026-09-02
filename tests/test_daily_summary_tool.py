"""CLI настройки утренней сводки (issue #301, spec 0035 §8).

Единственный путь записи `daily_summary_chat_id` и `daily_summary_local_time` в
Phase 0 (онбординг эти два поля не трогает — id чата узнают, добавив бота в
группу), поэтому покрыт: запись, показ, выключение и отказ на негодном времени.
Нужен работающий Postgres — как у всех DB-тестов.
"""

from __future__ import annotations

import pytest

from hospitality.platform.config import load_tenant_config
from hospitality.platform.seed import DEMO_TENANT_SLUG, seed_demo_tenant
from hospitality.shared.db import platform_session_scope
from hospitality.tools.daily_summary import DailySummaryConfigError, apply_settings, main
from hospitality.tools.seed import seed_demo_data

pytestmark = pytest.mark.usefixtures("canonical_database")


async def _stored() -> tuple[str | None, str]:
    tenant_id = await seed_demo_tenant()  # идемпотентен: тенант уже есть
    async with platform_session_scope() as session:
        config = await load_tenant_config(session, tenant_id)
    return config.daily_summary_chat_id, config.daily_summary_local_time


async def test_writes_chat_and_time() -> None:
    await seed_demo_data()

    written = await apply_settings(DEMO_TENANT_SLUG, chat_id="-1001", local_time="08:30", off=False)

    assert written == ("-1001", "08:30")
    assert await _stored() == ("-1001", "08:30")


async def test_without_arguments_only_shows() -> None:
    """Команда без аргументов ничего не меняет (канон `staff_routing`): «показать»
    и «задать» — разные намерения, и первое не должно случайно быть вторым."""
    await seed_demo_data()
    await apply_settings(DEMO_TENANT_SLUG, chat_id="-1001", local_time=None, off=False)

    assert await apply_settings(DEMO_TENANT_SLUG, chat_id=None, local_time=None, off=False) == (
        "-1001",
        "09:00",
    )
    assert await _stored() == ("-1001", "09:00")


async def test_off_switches_the_hotel_message_off_keeping_the_time() -> None:
    """`--off` убирает адресата, а не настройку времени: включат обратно — время
    останется тем, которое отель уже выбрал."""
    await seed_demo_data()
    await apply_settings(DEMO_TENANT_SLUG, chat_id="-1001", local_time="07:15", off=False)

    assert await apply_settings(DEMO_TENANT_SLUG, chat_id=None, local_time=None, off=True) == (
        None,
        "07:15",
    )


async def test_rejects_malformed_time_before_writing() -> None:
    """Негодное время — отказ с ненулевым кодом, а не строка в БД.

    `model_copy(update=...)` схему не перепроверяет, поэтому «9:00» лёг бы молча
    и упал бы уже у воркера в момент рассылки — там его увидит не тот, кто его
    написал.
    """
    await seed_demo_data()
    await apply_settings(DEMO_TENANT_SLUG, chat_id="-1001", local_time="08:30", off=False)

    with pytest.raises(DailySummaryConfigError):
        await apply_settings(DEMO_TENANT_SLUG, chat_id=None, local_time="9:00", off=False)
    assert await _stored() == ("-1001", "08:30")  # прежняя настройка цела


async def test_rejects_blank_chat_before_writing() -> None:
    """Пустой чат — тоже отказ: «выключено» выражают `--off`, а не пробелом."""
    await seed_demo_data()

    with pytest.raises(DailySummaryConfigError):
        await apply_settings(DEMO_TENANT_SLUG, chat_id="   ", local_time=None, off=False)


async def test_unknown_tenant_is_an_error() -> None:
    """Опечатка в slug не должна выглядеть успешной настройкой (канон
    `staff_routing`)."""
    with pytest.raises(DailySummaryConfigError):
        await apply_settings("no-such-hotel", chat_id="-1001", local_time=None, off=False)


def _stub_apply_settings(
    monkeypatch: pytest.MonkeyPatch, result: tuple[str | None, str] | DailySummaryConfigError
) -> list[dict[str, object]]:
    """Подменить работу с БД: `main` проверяется как разбор аргументов и печать
    (канон `test_staff_routing_tool`) — сама запись покрыта тестами выше."""
    calls: list[dict[str, object]] = []

    async def fake(
        tenant_slug: str, *, chat_id: str | None, local_time: str | None, off: bool
    ) -> tuple[str | None, str]:
        calls.append(
            {"tenant_slug": tenant_slug, "chat_id": chat_id, "local_time": local_time, "off": off}
        )
        if isinstance(result, DailySummaryConfigError):
            raise result
        return result

    monkeypatch.setattr("hospitality.tools.daily_summary.apply_settings", fake)
    return calls


def test_main_refuses_off_together_with_chat_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--off` и `--chat-id` вместе — два разных намерения; код возврата 2."""
    calls = _stub_apply_settings(monkeypatch, (None, "09:00"))

    assert main(["--off", "--chat-id", "-1001", "--tenant-slug=demo-hotel"]) == 2
    assert calls == []  # до БД дело не дошло


def test_main_prints_the_current_settings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Показ настроек — нулевой код возврата и строка оператору."""
    calls = _stub_apply_settings(monkeypatch, ("-1001", "08:30"))

    assert main(["--tenant-slug=demo-hotel"]) == 0

    assert calls == [
        {"tenant_slug": "demo-hotel", "chat_id": None, "local_time": None, "off": False}
    ]
    assert "уходит в чат -1001 в 08:30" in capsys.readouterr().out


def test_main_reports_error_to_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Отказ виден оператору текстом в stderr, а не трассировкой."""
    _stub_apply_settings(monkeypatch, DailySummaryConfigError("Негодное значение: 9:00"))

    assert main(["--at", "9:00", "--tenant-slug=demo-hotel"]) == 1
    assert "Негодное значение: 9:00" in capsys.readouterr().err


def test_main_says_when_the_hotel_gets_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Пустой чат — не молчание, а прямая строка: «отелю не уходит»."""
    _stub_apply_settings(monkeypatch, (None, "09:00"))

    assert main(["--off", "--tenant-slug=demo-hotel"]) == 0
    assert "Сводка отелю не уходит" in capsys.readouterr().out
