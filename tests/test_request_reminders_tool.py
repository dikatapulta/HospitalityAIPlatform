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
    apply_guest_raise_after,
    apply_reminders,
    guest_raise_text,
    main,
    parse_guest_raise_after,
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


async def test_stalled_mode_writes_its_own_pair() -> None:
    """`stalled` пишет поля «долго в работе» и не трогает невзятую (spec 0028 §8)."""
    await seed_demo_data()

    assert await apply_reminders(DEMO_TENANT_SLUG, stalled=True) == (None, {})
    result = await apply_reminders(
        DEMO_TENANT_SLUG, after_minutes=240, minutes_by_category={"maintenance": 360}, stalled=True
    )
    assert result == (240, {"maintenance": 360})

    config = await _stored_config()
    assert config.request_stalled_after_minutes == 240
    assert config.request_stalled_minutes_by_category == {"maintenance": 360}
    assert config.request_reminder_after_minutes == 30  # невзятая — как была

    assert await apply_reminders(DEMO_TENANT_SLUG, off=True, stalled=True) == (None, {})
    assert (await _stored_config()).request_reminder_after_minutes == 30


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
        stalled: bool = False,
    ) -> tuple[int | None, dict[str, int]]:
        calls.append(
            {
                "tenant_slug": tenant_slug,
                "after_minutes": after_minutes,
                "minutes_by_category": minutes_by_category,
                "off": off,
                "stalled": stalled,
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
            "stalled": False,
        }
    ]
    output = capsys.readouterr().out
    assert "Базовый срок: 20 мин." in output
    assert "maintenance → 10 мин" in output


def test_main_stalled_mode_prints_its_own_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _stub_apply_reminders(monkeypatch, (240, {"maintenance": 360}))

    assert main(["--stalled", "--after-minutes", "240", "maintenance=360"]) == 0

    assert calls[0]["stalled"] is True
    output = capsys.readouterr().out
    assert "«долго в работе»: 240 мин от взятия" in output
    assert "maintenance → 360 мин" in output


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


# ---------------------------------------------------------------------------
# --guest-raise-after (spec 0070 §6)
# ---------------------------------------------------------------------------


def test_parse_guest_raise_after() -> None:
    assert parse_guest_raise_after("3") == 3
    assert parse_guest_raise_after(" OFF ") is None
    for bad in ("0", "11", "два", ""):
        with pytest.raises(ReminderConfigError):
            parse_guest_raise_after(bad)


async def test_guest_raise_after_writes_only_its_field() -> None:
    await seed_demo_data()
    await apply_reminders(DEMO_TENANT_SLUG, after_minutes=20)

    assert await apply_guest_raise_after(DEMO_TENANT_SLUG, 3) == 3
    config = await _stored_config()
    assert (config.guest_reminder_raise_after, config.request_reminder_after_minutes) == (3, 20)

    assert await apply_guest_raise_after(DEMO_TENANT_SLUG, None) is None
    assert (await _stored_config()).guest_reminder_raise_after is None


def test_guest_raise_text_names_both_rules() -> None:
    """Вывод называет оба правила подъёма: по числу и по просрочке."""
    assert guest_raise_text(2) == (
        "Заявка поднимается к ресепшену и менеджеру со второго напоминания гостя, "
        "а уже просроченная — с первого."
    )
    assert "с третьего напоминания" in guest_raise_text(3)
    assert "выключен" in guest_raise_text(None)


def test_main_guest_raise_after_is_its_own_flag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, int | None]] = []

    async def fake(tenant_slug: str, raise_after: int | None) -> int | None:
        calls.append((tenant_slug, raise_after))
        return raise_after

    monkeypatch.setattr("hospitality.tools.request_reminders.apply_guest_raise_after", fake)

    assert main(["--guest-raise-after", "off", "--tenant-slug=demo-hotel"]) == 0
    assert calls == [("demo-hotel", None)]
    assert "выключен" in capsys.readouterr().out
    assert main(["--guest-raise-after", "3", "--stalled"]) == 2
    assert main(["--guest-raise-after", "12"]) == 1
    assert "от 1 до 10" in capsys.readouterr().err
