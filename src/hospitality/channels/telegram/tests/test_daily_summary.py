"""Утреннее сообщение со сводкой дня (issue #301, spec 0035 §8, блок §13).

Проверяет прогон целиком (`send_daily_summaries`) — так его зовёт воркер: гейт
локального времени отеля, ровно одно сообщение на сутки каждому адресату (P-8),
копия основателю со строкой расхода и отельная сводка без неё.

Час прогона задаётся подменой `utc_now` ВНУТРИ этого модуля, и подменяется он на
момент, посчитанный от НАСТОЯЩЕГО дня отеля: заявки, созданные тестом, получают
`service_day` по реальным часам, и сводка обязана считать именно их. Прыжок в
выдуманную дату разошёлся бы с данными и проверял бы пустой день.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import text

from hospitality.ai.escalation import EscalationContext, EscalationReason
from hospitality.channels.common.events import publish_escalation
from hospitality.channels.common.store import ensure_conversation
from hospitality.channels.telegram import daily_summary
from hospitality.channels.telegram.daily_summary import send_daily_summaries
from hospitality.channels.telegram.normalize import CHANNEL
from hospitality.channels.telegram.tests.conftest import RecordingSender
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import (
    HotelProfile,
    TenantConfig,
    list_configured_tenant_ids,
    store_tenant_config,
)
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.tenancy import tenant_context

HOTEL_CHAT = "-100500"
ALERT_CHAT = "-100999"
ZONE = ZoneInfo("Asia/Almaty")


class RecordingAlerts:
    """Фейк тракта алертов (`AlertSender`): копит тексты копий основателю."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, text: str) -> None:
        self.sent.append(text)


async def _configure(
    tenant_id: uuid.UUID,
    *,
    chat_id: str | None = HOTEL_CHAT,
    local_time: str = "09:00",
    reminder_minutes: int | None = 30,
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
                request_reminder_after_minutes=reminder_minutes,
                daily_summary_chat_id=chat_id,
                daily_summary_local_time=local_time,
            ),
        )


def _hotel_moment(hour: int, minute: int = 0, *, days_ahead: int = 1) -> datetime:
    """UTC-момент локального часа отеля через `days_ahead` суток.

    По умолчанию завтра: сводка рассказывает про ВЧЕРА, а заявки тест создаёт
    сегодня — значит прогон обязан случиться на следующий день отеля.
    """
    local_day = utc_now().astimezone(ZONE).date() + timedelta(days=days_ahead)
    return datetime.combine(local_day, time(hour, minute), tzinfo=ZONE).astimezone(UTC)


def _freeze(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    """Подменить часы ПРОГОНА (не всей системы): гейт «наступило ли утро»."""
    monkeypatch.setattr(daily_summary, "utc_now", lambda: moment)


async def _make_request(
    tenant_id: uuid.UUID,
    *,
    origin: requests_api.ServiceRequestOrigin = requests_api.ServiceRequestOrigin.GUEST_CHAT,
    category_key: str = "housekeeping",
    category_name: str = "Уборка",
    summary: str = "нужны полотенца",
) -> requests_api.ServiceRequestRead:
    """Заявка тенанта в статусе `new`, созданная сегодняшними сутками отеля."""
    with tenant_context(tenant_id):
        categories = {category.key: category for category in await requests_api.list_categories()}
        category = categories.get(category_key)
        if category is None:
            category = await requests_api.create_category(
                requests_api.RequestCategoryCreate(key=category_key, name=category_name)
            )
        return await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category.id,
                origin=origin,
                summary=summary,
                room_number="305",
            )
        )


async def _close(
    tenant_id: uuid.UUID,
    request: requests_api.ServiceRequestRead,
    status: requests_api.RequestStatus,
) -> None:
    """Закрыть заявку каноническим путём: карта переходов не обходится (P-5).

    В `done` — только через `in_progress` (заявку сперва берут в работу), в
    `cancelled` — из любого статуса.
    """
    with tenant_context(tenant_id):
        if status is requests_api.RequestStatus.DONE:
            await requests_api.change_request_status(
                request.id, requests_api.RequestStatus.IN_PROGRESS
            )
        await requests_api.change_request_status(request.id, status)


async def _run(
    sender: RecordingSender,
    *,
    alerts: RecordingAlerts | None = None,
    alert_chat_id: str = ALERT_CHAT,
) -> int:
    return await send_daily_summaries(
        sender=sender,
        alert_sender=alerts,
        alert_chat_id=alert_chat_id if alerts is not None else "",
    )


async def test_summary_is_sent_once_per_hotel_day(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DoD §13: сводка уходит в чат менеджера и ровно один раз на сутки отеля.

    Второй прогон в том же окне (а он случается каждые 10 минут до полуночи)
    не шлёт ничего — ключ `staff:daily_summary:<день>` уже записан (P-8).
    """
    await _configure(demo_tenant)
    await _make_request(demo_tenant)
    _freeze(monkeypatch, _hotel_moment(9))

    sender = RecordingSender()
    assert await _run(sender) == 1
    assert await _run(sender) == 0
    assert len(sender.sent) == 1

    chat_id, message = sender.sent[0]
    assert chat_id == HOTEL_CHAT
    assert message.startswith("📊 Вчера, ")
    assert "Заявки: 1 создано, 0 закрыто." in message
    assert "Откуда пришли: 1 — от гостей через бота, 0 — приняты вручную." in message
    assert "• Уборка — 1 → 0" in message
    assert "Открыто сейчас: 1." in message
    # Кнопок у сводки нет: это отчёт, а не заявка, по которой надо действовать.
    assert sender.markups == [None]


async def test_busy_day_prints_the_closed_breakdown_and_all_three_sources(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Две главные строки сводки — целиком, на дне, где есть всё (§9).

    Первый тест файла смотрит на вырожденный день: одна заявка, ни одной
    закрытой, ни одной из внешней системы. Полные формы обеих строк — разбивка
    закрытых «(N выполнено, M отменено)» и третий источник «N — из внешней
    системы» (issue #313) — не исполнялись при этом ни одним тестом: сводка
    могла уехать менеджеру с переставленными «выполнено»/«отменено», а строка
    «Откуда пришли» — не сойтись с «создано», и CI не заметил бы ни того, ни
    другого (ревью PR #329, Н-2 и Н-3).

    Числа взяты РАЗНЫЕ (6/3/2/1 и 2/1/3) намеренно: на равных перестановка
    подписей местами осталась бы зелёной.
    """
    await _configure(demo_tenant)
    guest_first = await _make_request(demo_tenant)
    await _make_request(demo_tenant)
    manual = await _make_request(demo_tenant, origin=requests_api.ServiceRequestOrigin.STAFF_MANUAL)
    from_api = [
        await _make_request(demo_tenant, origin=requests_api.ServiceRequestOrigin.API)
        for _ in range(3)
    ]
    await _close(demo_tenant, guest_first, requests_api.RequestStatus.DONE)
    await _close(demo_tenant, from_api[0], requests_api.RequestStatus.DONE)
    await _close(demo_tenant, manual, requests_api.RequestStatus.CANCELLED)
    _freeze(monkeypatch, _hotel_moment(9))

    sender = RecordingSender()
    assert await _run(sender) == 1

    message = sender.sent[0][1]
    assert "Заявки: 6 создано, 3 закрыто (2 выполнено, 1 отменено)." in message
    assert (
        "Откуда пришли: 2 — от гостей через бота, 1 — приняты вручную, "
        "3 — из внешней системы." in message
    )
    assert "Открыто сейчас: 3." in message


async def test_summary_waits_for_the_local_time_of_the_hotel(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Не уходит до наступления локального времени отеля — и уходит после.

    Пара прогонов, а не один: тест «в 08:59 тихо» в одиночку зеленел бы и на
    коде, который не шлёт вообще никогда.
    """
    await _configure(demo_tenant, local_time="09:00")
    await _make_request(demo_tenant)

    sender = RecordingSender()
    _freeze(monkeypatch, _hotel_moment(8, 59))
    assert await _run(sender) == 0
    assert sender.sent == []

    _freeze(monkeypatch, _hotel_moment(9, 0))
    assert await _run(sender) == 1


async def test_hotel_without_chat_gets_no_message(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`daily_summary_chat_id` пуст — сообщение отелю не уходит вовсе (§8).

    Страница кабинета при этом работает: настройка выключает рассылку, а не
    саму сводку.
    """
    await _configure(demo_tenant, chat_id=None)
    await _make_request(demo_tenant)
    _freeze(monkeypatch, _hotel_moment(9))

    sender = RecordingSender()
    assert await _run(sender) == 0
    assert sender.sent == []


async def test_founder_copy_carries_the_spend_line_and_the_hotel_one_does_not(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§13: копия основателю содержит строку расхода, отельная сводка — нет.

    Расход берётся у владельца журнала вызовов (`ai/gateway`) за то же окно
    суток отеля, что и числа заявок.
    """
    await _configure(demo_tenant)
    await _make_request(demo_tenant)
    await _log_llm_call(demo_tenant, cost_usd=Decimal("1.84"))
    _freeze(monkeypatch, _hotel_moment(9))

    sender = RecordingSender()
    alerts = RecordingAlerts()
    assert await _run(sender, alerts=alerts) == 2

    hotel_text = sender.sent[0][1]
    copy_text = alerts.sent[0]
    assert "ИИ за сутки отеля" not in hotel_text
    assert "$" not in hotel_text
    assert copy_text.startswith("🏨 Demo Hotel\n📊 Вчера, ")
    assert copy_text.endswith("ИИ за сутки отеля: $1.84")
    # Тело копии — тот же текст, что у отеля: расхождение сделало бы две сводки
    # об одном дне, и сверить их было бы нечем.
    assert hotel_text in copy_text


async def test_founder_copy_is_sent_once_even_without_a_hotel_chat(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """issue #306: у копии свой ключ идемпотентности, а не общий с отельной.

    Пустой `daily_summary_chat_id` — рабочее состояние (§8). Будь ключ один на
    обоих адресатов, он не записывался бы никогда, а условие «утро наступило»
    истинно до полуночи: копия ушла бы в алерт-чат ~90 раз за сутки, утопив в
    себе ERR-OPS-*, dead-letter и свежесть бэкапов.
    """
    await _configure(demo_tenant, chat_id=None)
    await _make_request(demo_tenant)
    _freeze(monkeypatch, _hotel_moment(9))

    sender = RecordingSender()
    alerts = RecordingAlerts()
    assert await _run(sender, alerts=alerts) == 1
    assert await _run(sender, alerts=alerts) == 0
    assert len(alerts.sent) == 1
    assert sender.sent == []


async def test_copy_is_not_sent_when_alerting_is_off(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Тракт алертов не настроен — копия не уходит, отельная сводка не страдает."""
    await _configure(demo_tenant)
    await _make_request(demo_tenant)
    _freeze(monkeypatch, _hotel_moment(9))

    sender = RecordingSender()
    assert await _run(sender, alerts=None) == 1
    assert len(sender.sent) == 1


async def test_empty_day_is_a_single_line(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пустой день — «Вчера, N месяца. Заявок не было.» (§9).

    Предикат — ВСЕ числа нули, включая эскалации: перечислять шесть нулей
    менеджеру незачем.
    """
    await _configure(demo_tenant)
    _freeze(monkeypatch, _hotel_moment(9))

    sender = RecordingSender()
    assert await _run(sender) == 1
    assert sender.sent[0][1].endswith(". Заявок не было.")


async def test_day_with_only_an_escalation_is_not_empty(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Заявок ноль, но бот звал сотрудника — день не пустой (§9).

    Короткая форма здесь соврала бы: «заявок не было» правда, а «ничего не
    происходило» — нет.
    """
    await _configure(demo_tenant)
    with tenant_context(demo_tenant):
        conversation_id = await ensure_conversation(CHANNEL, "guest-1")
        await publish_escalation(
            conversation_id,
            uuid.uuid4(),
            chat_id="guest-1",
            guest_message="позовите человека",
            escalation=EscalationContext(
                reason=EscalationReason.LLM_UNAVAILABLE, error_code="ERR-AI-002"
            ),
        )
    _freeze(monkeypatch, _hotel_moment(9))

    sender = RecordingSender()
    assert await _run(sender) == 1
    message = sender.sent[0][1]
    assert "Заявок не было" not in message
    assert "Бот звал сотрудника: 1 раз." in message


async def test_failed_send_repeats_on_the_next_run(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой отправки (ERR-TELEGRAM-007) не пишет ключ — следующий прогон повторит.

    Своей очереди у сводки нет (§8), и повтор держится ровно на этом: ключ
    пишется только после успешной отправки.
    """
    await _configure(demo_tenant)
    await _make_request(demo_tenant)
    _freeze(monkeypatch, _hotel_moment(9))

    class FailingOnceSender(RecordingSender):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        async def send_message(
            self, chat_id: str, text: str, *, reply_markup: dict[str, Any] | None = None
        ) -> str | None:
            if not self.failed:
                self.failed = True
                raise RuntimeError("Bot API 429")
            return await super().send_message(chat_id, text, reply_markup=reply_markup)

    sender = FailingOnceSender()
    assert await _run(sender) == 0
    assert await _run(sender) == 1


async def test_failed_hotel_send_does_not_cancel_or_consume_the_founder_copy(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ключей два, и это видно ровно здесь (issue #306): бот отеля лежит.

    Копия основателю уходит тем же прогоном — адресаты независимы. А на
    следующем прогоне отель получает свою сводку, и копия при этом НЕ уходит
    второй раз. С одним ключом на обоих случилось бы одно из двух: успех копии
    пометил бы сводку отеля отправленной (отель не получил бы её никогда), либо
    повтор отельной отправки продублировал бы копию.
    """
    await _configure(demo_tenant)
    await _make_request(demo_tenant)
    _freeze(monkeypatch, _hotel_moment(9))

    class BrokenSender(RecordingSender):
        async def send_message(
            self, chat_id: str, text: str, *, reply_markup: dict[str, Any] | None = None
        ) -> str | None:
            raise RuntimeError("Bot API 403")

    alerts = RecordingAlerts()
    assert await _run(BrokenSender(), alerts=alerts) == 1
    assert len(alerts.sent) == 1

    healthy = RecordingSender()
    assert await _run(healthy, alerts=alerts) == 1
    assert len(healthy.sent) == 1  # отель получил сводку следующим прогоном
    assert len(alerts.sent) == 1  # копия второй раз не ушла


async def test_tenant_without_config_is_skipped(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Тенант без конфига (онбординг не завершён) пропускается: адресата нет."""
    await _make_request(demo_tenant)
    _freeze(monkeypatch, _hotel_moment(9))

    sender = RecordingSender()
    assert await _run(sender) == 0


async def test_broken_tenant_config_does_not_stop_the_run(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Дрейф схемы конфига у одного отеля не отменяет сводку у остальных."""
    broken, healthy = two_tenants
    await _configure(healthy)
    await _make_request(healthy)
    async with platform_session_scope() as session:
        await session.execute(
            text("UPDATE tenants SET config = '{\"schema_version\": 1}'::jsonb WHERE id = :id"),
            {"id": broken},
        )
    _freeze(monkeypatch, _hotel_moment(9))

    sender = RecordingSender()
    assert await _run(sender) == 1


def _failed_summaries() -> float:
    """Счётчик неудач сводки: глобален на процесс, поэтому сравнивается приращение."""
    value = REGISTRY.get_sample_value("daily_summary_sent_total", {"outcome": "failed"})
    return value if value is not None else 0.0


async def test_unexpected_failure_on_one_tenant_does_not_stop_the_run(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Прогон обязан обойти всех — в том числе когда падает не `AppError`.

    Обработчик уровня прогона — не тот, что ловит дрейф конфига: тот ловит
    `AppError` ВНУТРИ `_send_for_tenant` и до верхнего не доходит (тест выше).
    Здесь падает всё остальное — оборванное в этот момент соединение с БД,
    битые данные, — и такой сбой не должен стоить сводки остальным отелям
    (ревью PR #329, Н-4). Заодно проверяется метка дежурного: сбой обязан
    попасть в `daily_summary_sent_total{outcome="failed"}` (ERR-TELEGRAM-007).
    """
    broken, healthy = two_tenants
    await _configure(broken, chat_id="-1001")
    await _configure(healthy)
    await _make_request(healthy)
    _freeze(monkeypatch, _hotel_moment(9))

    real_scan = list_configured_tenant_ids
    real_send = daily_summary._send_for_tenant

    async def broken_first(session: Any) -> list[uuid.UUID]:
        """Порядок обхода закрепить: упади сломанный последним, «прогон пошёл
        дальше» не проверялось бы вовсе — очередь `list_configured_tenant_ids`
        внутри одной транзакции решает id, то есть случай."""
        found = await real_scan(session)
        assert {broken, healthy} <= set(found)
        return sorted(found, key=lambda tenant_id: tenant_id != broken)

    async def fail_on_broken(tenant_id: uuid.UUID, **kwargs: Any) -> int:
        if tenant_id == broken:
            raise RuntimeError("соединение с БД оборвалось на середине прогона")
        return await real_send(tenant_id, **kwargs)

    monkeypatch.setattr(daily_summary, "list_configured_tenant_ids", broken_first)
    monkeypatch.setattr(daily_summary, "_send_for_tenant", fail_on_broken)

    failed_before = _failed_summaries()
    sender = RecordingSender()
    assert await _run(sender) == 1
    assert sender.sent[0][0] == HOTEL_CHAT  # сводку получил второй отель
    assert _failed_summaries() == failed_before + 1


async def test_two_tenants_do_not_see_each_others_numbers(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-4: у каждого отеля свои числа и свой чат — RLS, а не фильтр в коде."""
    first, second = two_tenants
    await _configure(first, chat_id="-1001")
    await _configure(second, chat_id="-1002")
    await _make_request(first)
    await _make_request(first)
    await _make_request(second)
    _freeze(monkeypatch, _hotel_moment(9))

    sender = RecordingSender()
    assert await _run(sender) == 2
    by_chat = dict(sender.sent)
    assert "Заявки: 2 создано" in by_chat["-1001"]
    assert "Заявки: 1 создано" in by_chat["-1002"]


def test_median_line_states_the_absence_instead_of_a_zero() -> None:
    """Медианы не существует, когда в этот день не брали ни одной заявки (§9).

    «половину взяли быстрее чем за 0 сек» читалось бы как «брали мгновенно» —
    ровно наоборот тому, что произошло.
    """
    assert daily_summary._median_line(360) == (
        "Из тех, что брали в работу, половину взяли быстрее чем за 6 мин."
    )
    assert daily_summary._median_line(None) == "В работу в этот день не брали ни одной заявки."


def test_overdue_line_distinguishes_zero_from_no_deadline() -> None:
    """Ноль просрочек и отсутствие сроков — разные вещи (§6).

    У отеля с выключенными напоминаниями просрочки не существует как явления, и
    «0» на этом месте означало бы «сроки соблюдены».
    """
    assert daily_summary._overdue_line(3) == (
        "Просрочено за день: 3 — взяли позже срока или не взяли вовремя."
    )
    assert daily_summary._overdue_line(0) == "Просрочено за день: 0."
    assert (
        daily_summary._overdue_line(None)
        == "Просрочено за день: не считается — напоминания выключены."
    )


@pytest.mark.parametrize(
    ("count", "expected"),
    [(0, "раз"), (1, "раз"), (2, "раза"), (4, "раза"), (5, "раз"), (11, "раз"), (22, "раза")],
)
def test_times_word_declension(count: int, expected: str) -> None:
    """«звал 1 раз», «2 раза», «11 раз»: иначе каждое утро приходит «5 раз(а)»."""
    assert daily_summary._times_word(count) == expected


async def _log_llm_call(tenant_id: uuid.UUID, *, cost_usd: Decimal) -> None:
    """Строка журнала вызовов LLM — SQL'ом: писать её умеет только сам gateway
    (он же считает стоимость), а тесту канала внутренности чужого слоя закрыты."""
    with tenant_context(tenant_id):
        async with session_scope() as session:
            await session.execute(
                text(
                    "INSERT INTO llm_call_log (id, tenant_id, provider, model, prompt_hash, "
                    "status, input_tokens, output_tokens, cost_usd, latency_ms, created_at) "
                    "VALUES (:id, :tenant_id, 'anthropic', 'claude-sonnet-5', 'hash', 'ok', "
                    "10, 20, :cost_usd, 100, :created_at)"
                ),
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "cost_usd": cost_usd,
                    "created_at": utc_now(),
                },
            )
