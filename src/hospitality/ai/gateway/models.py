"""Журнал вызовов LLM (Task 0014, FOUNDATION §7.2): «каждый вызов виден в БД».

Тенантная таблица: канон RLS скопирован с модуля requests (models.py),
RLS-блок — в миграции 0007 (копия канона 0002). Строка пишется на КАЖДЫЙ
исход вызова — успех, исчерпанные таймауты, ошибка провайдера: без этого
не работают ни контроль расходов, ни бюджет тенанта, ни разбор инцидентов.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Enum, ForeignKey, Integer, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from hospitality.shared.db import Base, UTCDateTime, utc_now
from hospitality.shared.tenancy import current_tenant_id


class LlmCallStatus(enum.StrEnum):
    """Исход вызова: ok — ответ получен; timeout — ретраи исчерпаны;
    error — провайдер ответил ошибкой (не таймаутом)."""

    OK = "ok"
    TIMEOUT = "timeout"
    ERROR = "error"


# Канон колонки-enum (models.py модуля requests): VARCHAR со значениями .value,
# без native enum Postgres — изменение состава остаётся миграцией данных.
llm_call_status_column_type = Enum(
    LlmCallStatus,
    name="llm_call_status",
    native_enum=False,
    length=16,
    values_callable=lambda members: [member.value for member in members],
)


class LlmCallLog(Base):
    """Одна строка — один вызов LLM через gateway (§7.2).

    `prompt_hash` — sha256 канонической сериализации запроса: версия промпта
    для evals и разбора регрессий без хранения самого текста (PII, §7.6).
    `cost_usd` — по прайс-листу `MODEL_PRICING_USD_PER_MTOK` (service.py);
    по сумме за UTC-сутки работает бюджетный лимит тенанта.
    """

    __tablename__ = "llm_call_log"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    prompt_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[LlmCallStatus] = mapped_column(llm_call_status_column_type)
    input_tokens: Mapped[int] = mapped_column(Integer(), default=0)
    output_tokens: Mapped[int] = mapped_column(Integer(), default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal(0))
    latency_ms: Mapped[int] = mapped_column(Integer(), default=0)
    # index: бюджетный запрос service.py — сумма cost_usd тенанта за UTC-сутки.
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, index=True)
