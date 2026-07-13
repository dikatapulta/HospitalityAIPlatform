"""HTTP-вебхук канала Telegram (Task 0016, §8.4 подписи вебхуков, §11).

Эндпоинт аутентифицируется НЕ сервисным токеном (`/api/v1/*`, Task 0013), а
секретом вебхука Telegram: при `setWebhook` боту задаётся `secret_token`, и
Telegram присылает его в заголовке `X-Telegram-Bot-Api-Secret-Token` на каждом
запросе (§8.4 — механизм проверки подлинности вебхука у Telegram). Неверный или
отсутствующий секрет → 403, до разбора тела. Тенант ставится не middleware'ом
сервисного токена, а самим обработчиком по маппингу чата (`service.process_update`).

Секрет-зависимость проверяется ДО валидации тела (FastAPI решает Depends раньше
body): чужой запрос получает 403, а не 422 с формой полезной нагрузки.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from hospitality.channels.telegram.client import TelegramSender, build_telegram_sender
from hospitality.channels.telegram.schemas import TelegramUpdate, TelegramWebhookAck
from hospitality.channels.telegram.service import process_update
from hospitality.shared.config import get_settings
from hospitality.shared.errors import AppError, ErrorResponse
from hospitality.shared.middleware import get_correlation_id

# Код каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_TELEGRAM_BAD_SECRET = "ERR-TELEGRAM-001"

_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


async def verify_telegram_secret(
    secret_token: Annotated[str | None, Header(alias=_SECRET_HEADER)] = None,
) -> None:
    """Проверить секрет вебхука (§8.4); неверный/пустой → 403 `ERR-TELEGRAM-001`.

    Закрыто по умолчанию: пустой `TELEGRAM_WEBHOOK_SECRET` в окружении — не
    «пускать всех», а отвергать всё, пока секрет не задан (§11: эндпоинт рождается
    аутентифицированным). Сравнение — постоянного времени (не течёт длиной
    совпавшего префикса при подборе).
    """
    configured = get_settings().telegram_webhook_secret
    if (
        not configured
        or secret_token is None
        or not secrets.compare_digest(secret_token.encode(), configured.encode())
    ):
        raise AppError(
            code=ERR_TELEGRAM_BAD_SECRET,
            message="Invalid Telegram webhook secret",
            status_code=403,
        )


def get_telegram_sender() -> TelegramSender:
    """Отправитель ответов по умолчанию (тесты переопределяют через dependency_overrides)."""
    return build_telegram_sender(get_settings())


router = APIRouter(prefix="/channels/telegram", tags=["telegram"])


@router.post(
    "/webhook",
    dependencies=[Depends(verify_telegram_secret)],
    summary="Приём обновлений Telegram Bot API",
    responses={
        403: {
            "model": ErrorResponse,
            "description": "Нет или неверный секрет вебхука (ERR-TELEGRAM-001)",
        }
    },
)
async def telegram_webhook(
    request: Request,
    update: TelegramUpdate,
    sender: Annotated[TelegramSender, Depends(get_telegram_sender)],
) -> TelegramWebhookAck:
    correlation_id = get_correlation_id(request) or ""
    await process_update(update, sender=sender, correlation_id=correlation_id)
    return TelegramWebhookAck()
