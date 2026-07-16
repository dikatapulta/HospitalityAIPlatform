"""Smoke-сценарии гостя (Task 0019, §13.6) — инструмент приёмки основателя.

Каждый тест — один бизнес-сценарий; первая строка docstring печатается в
отчёте как есть. Система — чёрный ящик за HTTP (spec 0019): действия гостя —
вебхук Telegram, наблюдение результата — API заявок и /metrics. Модель —
настоящая: сценарии терпимы к её свободе (повторное подтверждение), но
не подменяют её.

Сценарий дубликата переиспользует диалог сценария уборки (иначе каждый прогон
платил бы за второй полный диалог с моделью): состояние передаётся через
_cleaning_flow, при провале первого сценария второй честно пропускается.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

from tests.smoke.conftest import SmokeSettings

# Сколько ждать появления заявки после подтверждения. Создание синхронно
# внутри вебхука, поэтому это страховка от сетевых задержек, а не очередей.
REQUEST_WAIT_SECONDS = 15.0
POLL_INTERVAL_SECONDS = 1.0

pytestmark = pytest.mark.smoke

# Артефакты сценария уборки для сценария дубликата (см. docstring модуля).
_cleaning_flow: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Действия гостя и наблюдение результата — только через публичный HTTP.
# ---------------------------------------------------------------------------


def _send_guest_message(
    client: httpx.Client, settings: SmokeSettings, update: dict[str, Any]
) -> None:
    response = client.post(
        "/channels/telegram/webhook",
        json=update,
        headers={"X-Telegram-Bot-Api-Secret-Token": settings.webhook_secret},
    )
    if response.status_code != 200:
        pytest.fail(
            f"Вебхук ответил {response.status_code} {response.text!r} — гость не может "
            "писать в систему. Проверь TELEGRAM_WEBHOOK_SECRET (403) и логи app (5xx).",
            pytrace=False,
        )


def _guest_update(update_id: int, chat_id: int, text: str) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "chat": {"id": chat_id}, "text": text},
    }


def _api_get(client: httpx.Client, settings: SmokeSettings, path: str) -> Any:
    response = client.get(path, headers={"Authorization": f"Bearer {settings.service_token}"})
    if response.status_code != 200:
        pytest.fail(
            f"API заявок ответил {response.status_code} на {path} — служба не увидит "
            "заявки. Проверь SERVICE_TOKEN и логи app.",
            pytrace=False,
        )
    return response.json()


def _list_requests(client: httpx.Client, settings: SmokeSettings) -> list[dict[str, Any]]:
    page = _api_get(client, settings, "/api/v1/requests?limit=100")
    items: list[dict[str, Any]] = page["items"]
    return items


def _category_keys_by_id(client: httpx.Client, settings: SmokeSettings) -> dict[str, str]:
    categories = _api_get(client, settings, "/api/v1/requests/categories")
    return {category["id"]: category["key"] for category in categories}


def _llm_success_calls(client: httpx.Client) -> float:
    """Сумма счётчика llm_calls_total со status="ok" по всем меткам."""
    response = client.get("/metrics")
    if response.status_code != 200:
        pytest.fail(
            f"/metrics ответил {response.status_code} — наблюдаемость (Task 0018) "
            "не работает, smoke не может проверить ответ AI.",
            pytrace=False,
        )
    total = 0.0
    for line in response.text.splitlines():
        if line.startswith("llm_calls_total{") and 'status="ok"' in line:
            total += float(line.rsplit(" ", 1)[1])
    return total


def _wait_for_new_request(
    client: httpx.Client,
    settings: SmokeSettings,
    known_ids: set[str],
    room: str,
    timeout_seconds: float,
) -> dict[str, Any] | None:
    """Ждать появления новой заявки про нашу комнату (id вне известных)."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        for item in _list_requests(client, settings):
            is_new = item["id"] not in known_ids
            about_our_room = item["room_number"] == room or room in (item["summary"] or "")
            if is_new and about_our_room:
                return item
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_INTERVAL_SECONDS)


def _cancel_request(client: httpx.Client, settings: SmokeSettings, request_id: str) -> None:
    """Убрать smoke-заявку из работы службы (new → cancelled)."""
    response = client.post(
        f"/api/v1/requests/{request_id}/status",
        json={"status": "cancelled"},
        headers={"Authorization": f"Bearer {settings.service_token}"},
    )
    if response.status_code != 200:
        pytest.fail(
            f"Не удалось отменить smoke-заявку {request_id}: {response.status_code} "
            f"{response.text!r} — жизненный цикл заявок сломан.",
            pytrace=False,
        )


# ---------------------------------------------------------------------------
# Сценарии.
# ---------------------------------------------------------------------------


def test_cleaning_request_reaches_service(
    client: httpx.Client, settings: SmokeSettings, run_id: int
) -> None:
    """Гость просит уборку → заявка появляется у службы.

    Два хода: просьба с номером комнаты → «да» на переспрос (гейт P-9). Если
    модель вместо предложения заявки уточнила детали — один повторный ход
    с явным подтверждением (терпимость к настоящей модели, spec 0019).
    """
    chat_id = run_id
    room = str(900 + run_id % 100)
    known_ids = {item["id"] for item in _list_requests(client, settings)}

    _send_guest_message(
        client,
        settings,
        _guest_update(run_id, chat_id, f"Здравствуйте! Нужна уборка в номере {room}, пожалуйста."),
    )
    confirm = _guest_update(run_id + 1, chat_id, "Да")
    _send_guest_message(client, settings, confirm)
    request = _wait_for_new_request(client, settings, known_ids, room, REQUEST_WAIT_SECONDS)

    if request is None:  # модель переспросила — подтверждаем ещё раз и ждём снова
        confirm = _guest_update(
            run_id + 2, chat_id, f"Да, подтверждаю: оформите уборку номера {room}."
        )
        _send_guest_message(client, settings, confirm)
        request = _wait_for_new_request(client, settings, known_ids, room, REQUEST_WAIT_SECONDS)

    if request is None:
        pytest.fail(
            f"Гость попросил уборку номера {room}, но заявка у службы так и не появилась. "
            "Проверь: ANTHROPIC_API_KEY, логи app (make dev-logs; на staging — "
            "docs/runbooks/alerts.md) и что make seed создал категории.",
            pytrace=False,
        )

    _cleaning_flow.update(request_id=request["id"], confirm_update=confirm, room=room)
    _cancel_request(client, settings, request["id"])

    category_key = _category_keys_by_id(client, settings).get(request["category_id"])
    if category_key != "housekeeping":
        pytest.fail(
            f"Заявка создана, но попала не в ту службу: категория {category_key!r} "
            "вместо housekeeping — AI неверно маршрутизирует просьбы об уборке.",
            pytrace=False,
        )
    assert request["status"] == "new", (
        f"Новая заявка родилась в статусе {request['status']!r} вместо new"
    )


def test_question_gets_answer(client: httpx.Client, settings: SmokeSettings, run_id: int) -> None:
    """Гость задаёт вопрос → получает ответ, лишняя заявка не создаётся.

    Ответ гостю уходит в Telegram-чат и снаружи не виден (ограничение v0,
    spec 0019), поэтому «получил ответ» проверяется по успешному вызову
    модели в /metrics, а «не наломала дров» — по списку заявок.
    """
    chat_id = run_id + 10  # свой чат: вопрос не должен попасть в диалог об уборке
    known_ids = {item["id"] for item in _list_requests(client, settings)}
    successes_before = _llm_success_calls(client)

    _send_guest_message(
        client,
        settings,
        _guest_update(run_id + 10, chat_id, "Здравствуйте! Во сколько у вас завтрак?"),
    )

    if _llm_success_calls(client) <= successes_before:
        pytest.fail(
            'AI не ответил на вопрос гостя (llm_calls_total со status="ok" не '
            "вырос). Проверь ANTHROPIC_API_KEY и логи app: гость сейчас получает "
            "заглушку о недоступности.",
            pytrace=False,
        )
    new_requests = [i for i in _list_requests(client, settings) if i["id"] not in known_ids]
    if new_requests:
        pytest.fail(
            f"Вопрос про завтрак породил {len(new_requests)} заявок(и) службе — AI "
            "создаёт заявки там, где нужен просто ответ.",
            pytrace=False,
        )


def test_duplicate_webhook_creates_single_request(
    client: httpx.Client, settings: SmokeSettings
) -> None:
    """Дубликат вебхука → заявка не дублируется.

    Telegram повторяет доставку апдейтов — повтор подтверждения из сценария
    уборки (тот же update_id) не должен создать вторую заявку (P-8).
    """
    if not _cleaning_flow:
        pytest.skip("сценарий уборки не прошёл — дубликат нечем проверять")

    known_ids = {item["id"] for item in _list_requests(client, settings)}
    _send_guest_message(client, settings, _cleaning_flow["confirm_update"])

    room = _cleaning_flow["room"]
    duplicates = [
        item
        for item in _list_requests(client, settings)
        if item["id"] not in known_ids
        and (item["room_number"] == room or room in (item["summary"] or ""))
    ]
    if duplicates:
        pytest.fail(
            "Повтор того же вебхука создал вторую заявку — идемпотентность (P-8) "
            "сломана: служба будет получать дубли на каждый ретрай Telegram.",
            pytrace=False,
        )
