"""CLI срока напоминаний о невзятых заявках (spec 0028, issue #57).

Единственный путь записи срока в конфиг тенанта в Phase 0, поэтому покрыт:
показ, запись базового срока и пер-категорийных по отдельности, отказ на
неизвестной категории и на сроке вне границ, `--off`. Канон — тесты CLI
маршрутизации (`test_staff_routing_tool.py`). Нужен работающий Postgres.
"""

from __future__ import annotations

import pytest

from hospitality.platform.config import TenantConfig, load_tenant_config
from hospitality.platform.seed import DEMO_TENANT_SLUG, seed_demo_tenant
from hospitality.shared.db import platform_session_scope
from hospitality.tools.request_reminders import (
    ReminderConfigError,
    apply_reminders,
    main,
    parse_pairs,
)
from hospitality.tools.seed import seed_demo_data

pytestmark = pytest.mark.usefixtures("canonical_database")


async def _stored_config() -> TenantConfig:
    tenant_id = await seed_demo_tenant()  # идемпотентен: тенант уже есть
    async with platform_session_scope() as session:
        return await load_tenant_config(session, tenant_id)


def test_parse_pairs_reads_key_and_minutes() -> None:
    assert parse_pairs(["maintenance=10", "housekeeping=45"]) == {
        "maintenance": 10,
        "housekeeping": 45,
    }


def test_parse_pairs_rejects_garbage() -> None:
    """Аргумент без «=» или с нечисловыми минутами — ошибка ввода, а не молча
    проигнорированная настройка."""
    for garbage in (["maintenance"], ["=10"], ["maintenance="], ["maintenance=скоро"]):
        with pytest.raises(ReminderConfigError):
            parse_pairs(garbage)


async def test_shows_current_values_without_writing() -> None:
    """Без аргументов правки — показ; платформенный дефолт виден как есть."""
    await seed_demo_data()

    assert await apply_reminders(DEMO_TENANT_SLUG) == (30, {})


async def test_writes_base_delay_only() -> None:
    """`--after-minutes` меняет базовый срок и не трогает пер-категорийные."""
    await seed_demo_data()
    await apply_reminders(DEMO_TENANT_SLUG, minutes_by_category={"maintenance": 10})

    assert await apply_reminders(DEMO_TENANT_SLUG, after_minutes=20) == (20, {"maintenance": 10})

    config = await _stored_config()
    assert config.request_reminder_after_minutes == 20
    assert config.request_reminder_minutes_by_category == {"maintenance": 10}


async def test_replaces_category_delays_wholesale() -> None:
    """Конфиг — значение: перечислили пары — ровно они и останутся (§6)."""
    await seed_demo_data()
    await apply_reminders(DEMO_TENANT_SLUG, minutes_by_category={"maintenance": 10})

    await apply_reminders(DEMO_TENANT_SLUG, minutes_by_category={"housekeeping": 45})

    assert (await _stored_config()).request_reminder_minutes_by_category == {"housekeeping": 45}


async def test_rejects_unknown_category() -> None:
    """Опечатка в ключе категории — отказ со списком доступных: молча принятая
    настройка выглядела бы рабочей, а служба ждала бы напоминаний зря."""
    await seed_demo_data()

    with pytest.raises(ReminderConfigError, match="maintenence"):
        await apply_reminders(DEMO_TENANT_SLUG, minutes_by_category={"maintenence": 10})

    assert (await _stored_config()).request_reminder_minutes_by_category == {}


async def test_rejects_delay_out_of_range() -> None:
    """Границы схемы (1..10080) действуют и на пути CLI: в БД не должен попасть
    срок, который потом упадёт на чтении конфига."""
    await seed_demo_data()

    with pytest.raises(ReminderConfigError):
        await apply_reminders(DEMO_TENANT_SLUG, after_minutes=0)

    assert (await _stored_config()).request_reminder_after_minutes == 30


async def test_off_clears_both_fields() -> None:
    """`--off` — «напоминаний у этого отеля нет», без остатков в категориях."""
    await seed_demo_data()
    await apply_reminders(
        DEMO_TENANT_SLUG, after_minutes=20, minutes_by_category={"maintenance": 10}
    )

    assert await apply_reminders(DEMO_TENANT_SLUG, off=True) == (None, {})

    config = await _stored_config()
    assert config.request_reminder_after_minutes is None
    assert config.request_reminder_minutes_by_category == {}


async def test_rejects_unknown_tenant() -> None:
    with pytest.raises(ReminderConfigError, match="не найден"):
        await apply_reminders("no-such-hotel", after_minutes=20)


def test_main_rejects_off_with_delays() -> None:
    """`--off` вместе со сроками — два разных намерения; ненулевой код возврата."""
    assert main(["--off", "maintenance=10"]) == 2
    assert main(["--off", "--after-minutes", "20"]) == 2


def _stub_apply_reminders(
    monkeypatch: pytest.MonkeyPatch,
    result: tuple[int | None, dict[str, int]] | ReminderConfigError,
) -> list[dict[str, object]]:
    """Подменить запись в БД на заглушку; вернуть журнал вызовов.

    `main` синхронный и сам зовёт `asyncio.run` — в БД он в тесте не ходит
    (чужой event loop): проверяется разбор аргументов и текст для оператора,
    а работа с конфигом покрыта тестами `apply_reminders` выше.
    """
    calls: list[dict[str, object]] = []

    async def fake(
        tenant_slug: str,
        *,
        after_minutes: int | None = None,
        minutes_by_category: dict[str, int] | None = None,
        off: bool = False,
    ) -> tuple[int | None, dict[str, int]]:
        calls.append(
            {
                "tenant_slug": tenant_slug,
                "after_minutes": after_minutes,
                "minutes_by_category": minutes_by_category,
                "off": off,
            }
        )
        if isinstance(result, ReminderConfigError):
            raise result
        return result

    monkeypatch.setattr("hospitality.tools.request_reminders.apply_reminders", fake)
    return calls


def test_main_passes_arguments_and_prints_result(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _stub_apply_reminders(monkeypatch, (20, {"maintenance": 10}))

    assert main(["maintenance=10", "--after-minutes", "20", "--tenant-slug=demo-hotel"]) == 0

    assert calls == [
        {
            "tenant_slug": "demo-hotel",
            "after_minutes": 20,
            "minutes_by_category": {"maintenance": 10},
            "off": False,
        }
    ]
    output = capsys.readouterr().out
    assert "Базовый срок: 20 мин." in output
    assert "maintenance → 10 мин" in output


def test_main_reports_disabled_reminders(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_apply_reminders(monkeypatch, (None, {}))

    assert main([]) == 0

    assert "выключены" in capsys.readouterr().out


def test_main_reports_input_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Оператор получает текст, а не трассировку; код возврата — ненулевой."""
    _stub_apply_reminders(monkeypatch, ReminderConfigError("Неизвестные категории: xxx"))

    assert main(["xxx=10"]) == 1

    assert "Неизвестные категории: xxx" in capsys.readouterr().err
