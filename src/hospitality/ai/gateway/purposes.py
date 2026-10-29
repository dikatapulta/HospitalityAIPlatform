"""Назначение вызова: своя модель и свой дневной бюджет (ADR-024 §1–2).

Гостевой бот, «Наставник» персонала (spec 0048) и чтение фото реквизитов для
АВР (`documents`, spec 0049 Р-13) ходят к модели через одну дверь, но платят из
разных кошельков: обучение персонала или поток фото не вправе съесть бюджет
гостя, и наоборот. Назначение — поле запроса (`LlmRequest.purpose`,
умолчание `"guest"`); здесь — таблица «назначение → модель и бюджет».

Новое назначение — ветка в `purpose_settings`, две настройки в
`shared/config.py`, проброс в compose (app и worker) и строка в README шлюза.
`assert_never` не даст забыть ветку: mypy strict видит неразобранное значение.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal, assert_never

from hospitality.shared.config import Settings
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

LlmPurpose = Literal["guest", "mentor", "documents"]

# Каталог ошибок (docs/runbooks/errors.md, R-8).
ERR_AI_PURPOSE_NOT_CONFIGURED: Final = "ERR-AI-006"


@dataclass(frozen=True)
class PurposeSettings:
    """Модель и дневной бюджет тенанта (USD, UTC-сутки) одного назначения.

    Пустая `model` — назначение выключено на этом деплойменте (ADR-024 §2).
    """

    model: str
    daily_budget_usd: Decimal


def purpose_settings(purpose: LlmPurpose, settings: Settings) -> PurposeSettings:
    """Модель и бюджет назначения — из настроек деплоймента."""
    match purpose:
        case "guest":
            return PurposeSettings(
                model=settings.llm_model,
                daily_budget_usd=Decimal(str(settings.llm_tenant_daily_budget_usd)),
            )
        case "mentor":
            return PurposeSettings(
                model=settings.llm_model_mentor,
                daily_budget_usd=Decimal(str(settings.llm_mentor_daily_budget_usd)),
            )
        case "documents":
            return PurposeSettings(
                model=settings.llm_model_documents,
                daily_budget_usd=Decimal(str(settings.llm_documents_daily_budget_usd)),
            )
        case _:
            assert_never(purpose)


def require_purpose(purpose: LlmPurpose, settings: Settings) -> PurposeSettings:
    """Настройки назначения или ERR-AI-006, если его модель на деплойменте не задана.

    Отказ — до резерва и до провайдера: вызова не было, платить не за что, и
    строки журнала нет (ADR-024 §2). WARNING, а не ERROR: это конфигурация
    деплоймента (dev и CI поднимаются без ключа OpenAI), а не сбой.
    """
    config = purpose_settings(purpose, settings)
    if not config.model:
        logger.warning("llm_purpose_not_configured", purpose=purpose)
        raise AppError(
            code=ERR_AI_PURPOSE_NOT_CONFIGURED,
            message=f"LLM purpose {purpose!r} has no model on this deployment",
            status_code=503,
        )
    return config
