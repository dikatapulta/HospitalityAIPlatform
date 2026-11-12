"""Прогон по эффектам (#1051 п. 1, `tools/effects_run`): путь запуска на демо-отеле из сида.

Демо-отель — тем же сидом, что на деплое staging, под признаками staging и общим
выключателем разделов: скрипт видит ту же картину, что увидит на сервере. Проверено:
все шаги ✓ на исправном отеле, уборка гасит всё созданное, упавший шаг не мешает
следующему пути и уборке, вне staging скрипт не пишет ничего.
F811 отключён на файл: параметр-фикстура обязан называться как реимпортированная фикстура.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from hospitality.modules.guests import api as guests_api
from hospitality.modules.mentor import api as mentor_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import TenantConfig, mutate_tenant_config
from hospitality.platform.models import StaffRole, UserStatus
from hospitality.platform.staff_auth import find_hotel
from hospitality.platform.staff_invites import list_pending_invites
from hospitality.platform.staff_services import RECEPTION_SERVICE_KEY
from hospitality.platform.staff_team import list_tenant_members
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.tests.conftest import _fake_rate_limit  # noqa: F401  (фикстура)
from hospitality.tools import effects_run
from hospitality.tools.demo_showcase_staff import ensure_reception_category
from hospitality.tools.effects_run_paths import TEST_ROOM
from hospitality.tools.seed import seed_demo_data
from tests.test_staff_auth import create_staff_user

STAGING_ENV = {
    "SENTRY_ENVIRONMENT": "staging",
    "TELEGRAM_TENANT_SLUG": "demo-hotel",
    "PUBLIC_BASE_URL": "https://staging.example.test",
    "STAGING_ALL_FEATURES": "true",
    # «Наставник» на staging — модель раздела из `STAGING_SECTION_MODELS` при ключе OpenAI;
    # ключ нужен только проверке старта: провайдеры прогона — Fake, в OpenAI вызов не уходит.
    "OPENAI_API_KEY": "test-key-never-sent",
    "LLM_EMBEDDING_MODEL": "text-embedding-3-small",
}
RECEPTION_PHONE = "+7 727 000-00-00"


@pytest.fixture
def staging_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name, value in STAGING_ENV.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def demo_hotel(canonical_database: None, staging_env: None) -> uuid.UUID:
    """Демо-отель сида с менеджером, службой эскалаций, телефоном и экзаменом из 2 вопросов."""
    await seed_demo_data()
    hotel = await find_hotel("demo-hotel")
    assert hotel is not None
    await create_staff_user("BOSS", tenant_id=hotel.tenant_id, role=StaffRole.MANAGER)
    with tenant_context(hotel.tenant_id):
        await ensure_reception_category()  # на staging её заводит demo_showcase (#785)
    await _configure(
        hotel.tenant_id,
        reception_phone=RECEPTION_PHONE,
        escalation_category_key=RECEPTION_SERVICE_KEY,
        mentor_exam_question_count=2,
    )
    return hotel.tenant_id


async def _configure(tenant_id: uuid.UUID, **update: object) -> None:
    def apply(config: TenantConfig) -> TenantConfig:
        return TenantConfig.model_validate({**config.model_dump(), **update})

    async with platform_session_scope() as session:
        await mutate_tenant_config(session, tenant_id, apply)


def _report_text(report: effects_run.Report) -> str:
    return "\n".join(report.lines())


async def test_run_passes_every_step_and_cleans_up(demo_hotel: uuid.UUID) -> None:
    report = await effects_run.run_effects()

    assert report.passed, _report_text(report)
    paths = {result.path for result in report.results}
    assert paths == {"Подготовка", "Сотрудник", "Гость", "Кабинет", "Уборка"}
    assert "Итог: все шаги ✓" in report.lines()
    await _assert_nothing_left(demo_hotel)


async def test_failed_step_stops_its_path_only_and_cleanup_still_runs(
    demo_hotel: uuid.UUID,
) -> None:
    await _configure(demo_hotel, escalation_category_key=None)

    report = await effects_run.run_effects()

    assert not report.passed
    guest = [result for result in report.results if result.path == "Гость"]
    assert guest[0].ok is False and "службы эскалаций" in guest[0].detail
    assert all(result.ok is None for result in guest[1:])
    staff = [result for result in report.results if result.path == "Сотрудник"]
    assert all(result.ok for result in staff), _report_text(report)
    assert all(r.ok for r in report.results if r.path == "Уборка"), _report_text(report)
    await _assert_nothing_left(demo_hotel)


async def _assert_nothing_left(tenant_id: uuid.UUID) -> None:
    with tenant_context(tenant_id):
        assert await guests_api.find_active_stay(TEST_ROOM) is None
        assert await mentor_api.list_exam_assignments() == []
        open_cards = await requests_api.list_open_requests(limit=100)
    assert [card for card in open_cards if card.room_number == TEST_ROOM] == []
    ours = [m for m in await list_tenant_members(tenant_id) if (m.login or "").startswith("EF")]
    assert {member.login[:3] for member in ours if member.login} == {"EFM", "EFS"}
    assert all(member.user_status is UserStatus.DEACTIVATED for member in ours)
    assert [i for i in await list_pending_invites(tenant_id) if i.login.startswith("EF")] == []


def test_refuses_outside_staging(staging_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # `main()` ставит корневому логгеру WARNING, и уровень пережил бы тест: соседи того же
    # процесса, ловящие INFO через caplog, видели бы пустой журнал (#1096).
    monkeypatch.setattr(effects_run, "configure_logging", lambda level: None)
    assert effects_run.main([]) == 2  # нет флага

    monkeypatch.setenv("TELEGRAM_TENANT_SLUG", "pilot-hotel")  # копия отеля
    get_settings.cache_clear()
    assert effects_run.main([effects_run.CONFIRM_FLAG]) == 2
