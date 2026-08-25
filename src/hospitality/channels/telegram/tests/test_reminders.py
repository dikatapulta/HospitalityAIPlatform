"""Напоминания о невзятых заявках (issue #57, spec 0028).

Проверяет прогон целиком (`remind_unclaimed_requests`) — так его зовёт воркер:
срок из конфига тенанта, адресат по категории, одно напоминание на заявку (P-8),
изоляция сбоев и тенантов.

Возраст заявки задаётся сдвигом `created_at` в БД: ждать реальные минуты в тестах
нельзя, а подменять «сейчас» — значит проверять не тот код, который поедет.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text

from hospitality.channels.telegram import reminders
from hospitality.channels.telegram.reminders import remind_unclaimed_requests
from hospitality.channels.telegram.tests.conftest import RecordingSender
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import (
    HotelProfile,
    TenantConfig,
    store_tenant_config,
)
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.logging import configure_logging
from hospitality.shared.tenancy import tenant_context

DEFAULT_CHAT = "999"


async def _configure(
    tenant_id: uuid.UUID,
    *,
    after_minutes: int | None = 30,
    minutes_by_category: dict[str, int] | None = None,
    staff_chats: dict[str, str] | None = None,
) -> None:
    """Записать тенанту конфиг каноническим путём (`store_tenant_config`)."""
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                staff_chats_by_category=staff_chats or {},
                request_reminder_after_minutes=after_minutes,
                request_reminder_minutes_by_category=minutes_by_category or {},
            ),
        )


async def _make_request(
    tenant_id: uuid.UUID,
    *,
    category_key: str = "housekeeping",
    category_name: str = "Уборка",
    summary: str = "нужны полотенца",
    age_minutes: int = 0,
) -> requests_api.ServiceRequestRead:
    """Заявка тенанта в статусе `new`, «созданная» age_minutes назад."""
    with tenant_context(tenant_id):
        categories = {category.key: category for category in await requests_api.list_categories()}
        category = categories.get(category_key)
        if category is None:
            category = await requests_api.create_category(
                requests_api.RequestCategoryCreate(key=category_key, name=category_name)
            )
        request = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category.id,
                origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
                summary=summary,
                room_number="305",
            )
        )
        if age_minutes:
            # Возраст двигаем SQL'ом: домен менять `created_at` не умеет (и не
            # должен), а импортировать его ORM-модель тестам канала запрещено
            # (R-5). RLS всё равно ограничивает UPDATE текущим тенантом.
            async with session_scope() as session:
                await session.execute(
                    text("UPDATE service_requests SET created_at = :created_at WHERE id = :id"),
                    {"created_at": utc_now() - timedelta(minutes=age_minutes), "id": request.id},
                )
        return request


async def _scan(sender: RecordingSender, *, default_chat: str = DEFAULT_CHAT) -> int:
    return await remind_unclaimed_requests(sender=sender, default_staff_chat_id=default_chat)


async def test_unclaimed_request_is_reminded_to_its_service_chat(demo_tenant: uuid.UUID) -> None:
    """DoD issue #57: заявка, которую никто не взял дольше срока тенанта,
    подсвечивается повторным сообщением — в чат СВОЕЙ службы (spec 0026)."""
    await _configure(demo_tenant, staff_chats={"housekeeping": "-100777"})
    request = await _make_request(demo_tenant, age_minutes=45)

    sender = RecordingSender()
    assert await _scan(sender) == 1

    chat_id, text = sender.sent[0]
    assert chat_id == "-100777"  # чат уборки, а не общий
    assert f"#{request.daily_number}" in text
    assert "45 мин" in text  # возраст — чтобы было видно, сколько висит
    assert "Категория: Уборка" in text
    assert "Комната: 305" in text
    assert "Суть: нужны полотенца" in text
    # Кнопки статуса `new` (spec 0021 П-2): ноль ручного ввода.
    markup = sender.markups[0]
    assert markup is not None
    assert "Взять в работу" in str(markup)


async def test_fresh_request_is_not_reminded(demo_tenant: uuid.UUID) -> None:
    """Свежая заявка сканером не трогается: напоминание — про «висит», а не «создана»."""
    await _configure(demo_tenant)
    await _make_request(demo_tenant, age_minutes=5)

    sender = RecordingSender()
    assert await _scan(sender) == 0
    assert sender.sent == []


async def test_tenant_threshold_overrides_platform_default(demo_tenant: uuid.UUID) -> None:
    """Срок — конфигурация тенанта (P-11): 15 минут срабатывают там, где
    платформенные 30 ещё молчат."""
    await _configure(demo_tenant, after_minutes=15)
    await _make_request(demo_tenant, age_minutes=20)

    sender = RecordingSender()
    assert await _scan(sender) == 1


async def test_category_threshold_overrides_base(demo_tenant: uuid.UUID) -> None:
    """Свой срок на категорию (уборка ≠ прорыв трубы): инженерия напоминает
    через 10 минут, уборка на базовых 30 — ещё нет."""
    await _configure(demo_tenant, after_minutes=30, minutes_by_category={"maintenance": 10})
    await _make_request(
        demo_tenant,
        category_key="maintenance",
        category_name="Инженерия",
        summary="течёт кран",
        age_minutes=12,
    )
    await _make_request(demo_tenant, age_minutes=12)

    sender = RecordingSender()
    assert await _scan(sender) == 1
    assert "течёт кран" in sender.sent[0][1]


async def test_reminders_can_be_switched_off(demo_tenant: uuid.UUID) -> None:
    """`request_reminder_after_minutes = null` и пустой словарь — тенант выключил
    напоминания; сканер не шлёт ничего даже по давно висящей заявке."""
    await _configure(demo_tenant, after_minutes=None)
    await _make_request(demo_tenant, age_minutes=600)

    sender = RecordingSender()
    assert await _scan(sender) == 0
    assert sender.sent == []


async def test_reminder_is_sent_once(demo_tenant: uuid.UUID) -> None:
    """Одно напоминание на заявку (P-8): второй прогон её не трогает.

    Ключ `staff:request_unclaimed:<id>` пишется только после успешной отправки —
    та же опора идемпотентности, что у остальных уведомлений.
    """
    await _configure(demo_tenant)
    await _make_request(demo_tenant, age_minutes=45)

    sender = RecordingSender()
    assert await _scan(sender) == 1
    assert await _scan(sender) == 0
    assert len(sender.sent) == 1


async def test_claimed_request_is_not_reminded(demo_tenant: uuid.UUID) -> None:
    """Взятая в работу заявка напоминаний не получает: «никто не взял» — это
    ровно статус `new` (ADR-013)."""
    await _configure(demo_tenant)
    request = await _make_request(demo_tenant, age_minutes=45)
    with tenant_context(demo_tenant):
        await requests_api.change_request_status(request.id, requests_api.RequestStatus.IN_PROGRESS)

    sender = RecordingSender()
    assert await _scan(sender) == 0


async def test_category_without_mapping_falls_back_to_default_chat(demo_tenant: uuid.UUID) -> None:
    """Категория без своего чата → дефолтный чат (spec 0026)."""
    await _configure(demo_tenant, staff_chats={"maintenance": "-100777"})
    await _make_request(demo_tenant, age_minutes=45)

    sender = RecordingSender()
    assert await _scan(sender) == 1
    assert sender.sent[0][0] == DEFAULT_CHAT


async def test_scan_survives_missing_staff_chat(demo_tenant: uuid.UUID) -> None:
    """Ни чата категории, ни дефолтного (ERR-TELEGRAM-002): прогон не падает,
    заявка просто не уведомлена — и повторится, когда чат настроят."""
    await _configure(demo_tenant)
    await _make_request(demo_tenant, age_minutes=45)

    sender = RecordingSender()
    assert await _scan(sender, default_chat="") == 0
    assert sender.sent == []
    # Ключ не записан — после настройки чата напоминание уйдёт.
    assert await _scan(sender) == 1


async def test_send_failure_does_not_block_other_requests(demo_tenant: uuid.UUID) -> None:
    """Сбой отправки по одной заявке не отменяет остальные, а её напоминание
    повторится следующим прогоном (ключ не записан)."""
    await _configure(demo_tenant)
    await _make_request(demo_tenant, summary="первая", age_minutes=60)
    await _make_request(demo_tenant, summary="вторая", age_minutes=45)

    class FailingOnceSender(RecordingSender):
        """Первая попытка по «первой» падает, как упавший вызов Bot API."""

        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        async def send_message(
            self, chat_id: str, text: str, *, reply_markup: dict[str, Any] | None = None
        ) -> str | None:
            if "первая" in text and not self.failed:
                self.failed = True
                raise RuntimeError("Bot API 429")
            return await super().send_message(chat_id, text, reply_markup=reply_markup)

    sender = FailingOnceSender()
    # Порядок скана — новые сверху, но упавшая заявка не мешает второй.
    assert await _scan(sender) == 1
    assert await _scan(sender) == 1  # повтор упавшей на следующем прогоне
    assert {"первая", "вторая"} == {
        text.split("Суть: ")[1].split("\n")[0] for _, text in sender.sent
    }


async def test_truncated_scan_is_visible_in_logs(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Срез упёрся в `_SCAN_LIMIT` — это видно WARNING'ом.

    Молча усечённая выборка выглядит как «все просроченные обработаны», а на
    деле часть заявок скан не посмотрел (spec 0028 §3).
    """
    configure_logging()
    await _configure(demo_tenant)
    await _make_request(demo_tenant, summary="первая", age_minutes=60)
    await _make_request(demo_tenant, summary="вторая", age_minutes=45)
    monkeypatch.setattr(reminders, "_SCAN_LIMIT", 1)

    sender = RecordingSender()
    assert await _scan(sender) == 1  # вторая заявка в срез не попала

    events = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    assert any(event.get("event") == "unclaimed_request_scan_truncated" for event in events)
    # Итог прогона считает и кандидатов, и напоминания: без `candidates`
    # «просроченных не было» неотличимо от «были, но все уже напомнены».
    scanned = next(event for event in events if event.get("event") == "unclaimed_requests_scanned")
    assert scanned["candidates"] == 1
    assert scanned["reminded"] == 1
    assert scanned["tenants"] == 1


async def test_tenant_without_config_is_skipped(demo_tenant: uuid.UUID) -> None:
    """Тенант без конфига (онбординг не завершён) пропускается: срока у него нет."""
    await _make_request(demo_tenant, age_minutes=600)

    sender = RecordingSender()
    assert await _scan(sender) == 0
    assert sender.sent == []


async def test_broken_tenant_config_does_not_stop_the_scan(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Дрейф схемы конфига у одного отеля не отменяет напоминания у остальных.

    Конфиг там есть (тенант попадает в обход), но схему не проходит —
    ERR-PLATFORM-006. Такой тенант пропускается с WARNING, второй обрабатывается.
    """
    broken, healthy = two_tenants
    await _configure(healthy)
    await _make_request(healthy, age_minutes=45)
    async with platform_session_scope() as session:
        await session.execute(
            text("UPDATE tenants SET config = '{\"schema_version\": 1}'::jsonb WHERE id = :id"),
            {"id": broken},
        )

    sender = RecordingSender()
    assert await _scan(sender) == 1


async def test_age_older_than_a_day_is_shown_in_days(demo_tenant: uuid.UUID) -> None:
    """Пятые сутки читаются как «5 дн», а не «123 ч 40 мин»: крупная единица
    важнее точности — именно такие заявки и есть самые больные."""
    await _configure(demo_tenant)
    await _make_request(demo_tenant, age_minutes=5 * 24 * 60 + 3 * 60)

    sender = RecordingSender()
    assert await _scan(sender) == 1
    assert "5 дн 3 ч" in sender.sent[0][1]


async def test_failure_on_one_tenant_does_not_stop_the_others(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Скан обязан обойти всех: неожиданный сбой на одном отеле (битые данные,
    отвалившаяся в этот момент БД) логируется ERR-TELEGRAM-005 и не отменяет
    напоминания у остальных."""
    broken, healthy = two_tenants
    await _configure(broken)
    await _configure(healthy, staff_chats={"housekeeping": "-100BBB"})
    await _make_request(broken, age_minutes=45)
    await _make_request(healthy, age_minutes=45)

    original = reminders._remind_tenant

    async def failing_for_broken(tenant_id: uuid.UUID, **kwargs: object) -> tuple[int, int]:
        if tenant_id == broken:
            raise RuntimeError("connection reset")
        return await original(tenant_id, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(reminders, "_remind_tenant", failing_for_broken)

    sender = RecordingSender()
    assert await _scan(sender) == 1  # не бросает — иначе тест упал бы здесь
    assert sender.sent[0][0] == "-100BBB"


async def test_request_without_daily_number_falls_back_to_id(demo_tenant: uuid.UUID) -> None:
    """Доскелетная заявка (до миграции 0010) номера не имеет — напоминание
    остаётся действенным: в нём полный id и подсказка команд с ним."""
    await _configure(demo_tenant)
    request = await _make_request(demo_tenant, age_minutes=45)
    with tenant_context(demo_tenant):
        async with session_scope() as session:
            await session.execute(
                text("UPDATE service_requests SET daily_number = NULL WHERE id = :id"),
                {"id": request.id},
            )

    sender = RecordingSender()
    assert await _scan(sender) == 1
    text_sent = sender.sent[0][1]
    assert str(request.id) in text_sent
    assert "#" not in text_sent


async def test_reminders_are_tenant_isolated(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """P-4: заявка тенанта A не порождает уведомления в чате тенанта B."""
    tenant_a, tenant_b = two_tenants
    await _configure(tenant_a, staff_chats={"housekeeping": "-100AAA"})
    await _configure(tenant_b, staff_chats={"housekeeping": "-100BBB"})
    await _make_request(tenant_a, age_minutes=45)

    sender = RecordingSender()
    assert await _scan(sender) == 1
    assert sender.sent[0][0] == "-100AAA"
