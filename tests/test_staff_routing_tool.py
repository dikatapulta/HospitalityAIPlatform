"""CLI маршрутизации уведомлений по службам (spec 0026, issue #80).

Единственный путь записи маппинга `категория → чат` в Phase 0, поэтому покрыт:
запись, показ, отказ на неизвестной категории (опечатка не должна выглядеть
рабочей настройкой), очистка. Нужен работающий Postgres — как у всех DB-тестов.
"""

from __future__ import annotations

import pytest

from hospitality.platform.config import load_tenant_config
from hospitality.platform.seed import DEMO_TENANT_SLUG, seed_demo_tenant
from hospitality.shared.db import platform_session_scope
from hospitality.tools.seed import seed_demo_data
from hospitality.tools.staff_routing import RoutingError, apply_routing, main, parse_pairs

pytestmark = pytest.mark.usefixtures("canonical_database")


async def _stored_mapping() -> dict[str, str]:
    tenant_id = await seed_demo_tenant()  # идемпотентен: тенант уже есть
    async with platform_session_scope() as session:
        config = await load_tenant_config(session, tenant_id)
    return config.staff_chats_by_category


def test_parse_pairs_reads_key_value() -> None:
    assert parse_pairs(["housekeeping=-1001", "it-support=-1002"]) == {
        "housekeeping": "-1001",
        "it-support": "-1002",
    }


def test_parse_pairs_rejects_garbage() -> None:
    """Аргумент без «=» — ошибка ввода, а не молча проигнорированная настройка."""
    for garbage in (["housekeeping"], ["=-1001"], ["housekeeping="]):
        with pytest.raises(RoutingError):
            parse_pairs(garbage)


async def test_apply_routing_writes_and_reads_mapping() -> None:
    await seed_demo_data()  # тенант + категории (housekeeping, maintenance, it-support)

    written = await apply_routing(DEMO_TENANT_SLUG, {"housekeeping": "-1001"})

    assert written == {"housekeeping": "-1001"}
    assert await _stored_mapping() == {"housekeeping": "-1001"}
    # Без маппинга (None) — показ, а не перезапись.
    assert await apply_routing(DEMO_TENANT_SLUG, None) == {"housekeeping": "-1001"}


async def test_apply_routing_replaces_mapping_wholesale() -> None:
    """Конфиг — значение: перечислили пары — ровно они и останутся (§6)."""
    await seed_demo_data()
    await apply_routing(DEMO_TENANT_SLUG, {"housekeeping": "-1001"})

    await apply_routing(DEMO_TENANT_SLUG, {"maintenance": "-1002"})

    assert await _stored_mapping() == {"maintenance": "-1002"}


async def test_apply_routing_rejects_unknown_category() -> None:
    """Опечатка в ключе категории — отказ со списком доступных: молча принятая
    настройка выглядела бы рабочей, а уведомления шли бы в общий чат."""
    await seed_demo_data()

    with pytest.raises(RoutingError, match="housekeping"):
        await apply_routing(DEMO_TENANT_SLUG, {"housekeping": "-1001"})

    assert await _stored_mapping() == {}


async def test_apply_routing_clears_mapping() -> None:
    await seed_demo_data()
    await apply_routing(DEMO_TENANT_SLUG, {"housekeeping": "-1001"})

    assert await apply_routing(DEMO_TENANT_SLUG, {}) == {}
    assert await _stored_mapping() == {}


async def test_apply_routing_rejects_unknown_tenant() -> None:
    with pytest.raises(RoutingError, match="не найден"):
        await apply_routing("no-such-hotel", {"housekeeping": "-1001"})


def test_main_rejects_clear_with_pairs() -> None:
    """`--clear` вместе с парами — два разных намерения; ненулевой код возврата."""
    assert main(["--clear", "housekeeping=-1001"]) == 2


def _stub_apply_routing(
    monkeypatch: pytest.MonkeyPatch, result: dict[str, str] | RoutingError
) -> list[tuple[str, dict[str, str] | None]]:
    """Подменить запись в БД на заглушку; вернуть журнал вызовов.

    `main` синхронный и сам зовёт `asyncio.run` — в БД он в тесте не ходит
    (чужой event loop): проверяется разбор аргументов и текст для оператора,
    а работа с конфигом покрыта тестами `apply_routing` выше.
    """
    calls: list[tuple[str, dict[str, str] | None]] = []

    async def fake(tenant_slug: str, mapping: dict[str, str] | None) -> dict[str, str]:
        calls.append((tenant_slug, mapping))
        if isinstance(result, RoutingError):
            raise result
        return result

    monkeypatch.setattr("hospitality.tools.staff_routing.apply_routing", fake)
    return calls


def test_main_passes_pairs_and_prints_mapping(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _stub_apply_routing(monkeypatch, {"housekeeping": "-1001"})

    assert main(["housekeeping=-1001", "--tenant-slug=demo-hotel"]) == 0

    assert calls == [("demo-hotel", {"housekeeping": "-1001"})]
    assert "housekeeping → -1001" in capsys.readouterr().out


def test_main_without_arguments_only_shows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Без аргументов — показ (mapping=None), а не молчаливая очистка."""
    calls = _stub_apply_routing(monkeypatch, {})

    assert main(["--tenant-slug=demo-hotel"]) == 0

    assert calls == [("demo-hotel", None)]
    assert "Маппинг пуст" in capsys.readouterr().out


def test_main_clear_sends_empty_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_apply_routing(monkeypatch, {})

    assert main(["--clear", "--tenant-slug=demo-hotel"]) == 0

    assert calls == [("demo-hotel", {})]


def test_main_reports_error_to_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Отказ виден оператору текстом в stderr, а не трассировкой."""
    _stub_apply_routing(monkeypatch, RoutingError("Неизвестные категории: housekeping"))

    assert main(["housekeping=-1001", "--tenant-slug=demo-hotel"]) == 1
    assert "Неизвестные категории: housekeping" in capsys.readouterr().err
