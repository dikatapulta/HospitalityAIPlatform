"""Журнал вызовов LLM (Task 0014, FOUNDATION §7.2): «каждый вызов виден в БД».

Тенантные таблицы: канон RLS скопирован с модуля requests (models.py),
RLS-блоки — в миграциях 0007 и 0023 (копии канона 0002). Строка журнала
пишется на КАЖДЫЙ исход вызова — успех, исчерпанные таймауты, ошибка
провайдера: без этого не работают ни контроль расходов, ни бюджет тенанта,
ни разбор инцидентов. Рядом живёт `LlmBudgetReservation` — тот же бюджет,
но про вызовы, которые идут прямо сейчас и в журнале ещё не отразились.
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


class LlmBudgetReservation(Base):
    """Резерв дневного бюджета на время одного вызова LLM (issue #46, ADR-017).

    Журнал `llm_call_log` узнаёт стоимость только ПОСЛЕ ответа провайдера,
    поэтому вызовы, идущие прямо сейчас, для проверки бюджета невидимы —
    параллельные ходы гостей все читают «лимит не исчерпан» и все проходят
    (TOCTOU). Резерв делает такой вызов видимым до его начала: строка живёт
    от проверки бюджета до записи исхода в журнал.

    `reserved_until` — аренда по словарю ADR-016: умерший процесс не держит
    чужой бюджет вечно, истёкшие строки в сумму не входят и удаляются при
    следующем резерве того же тенанта. Оценка `amount_usd` пессимистична за
    счёт выхода (`max_tokens` резервируется целиком) — с запасом, покрывающим
    грубую оценку входа по длине строки; резерв обязан не занижать вызов, иначе
    он не ограничивает то, ради чего заведён (арифметика — ADR-017).
    """

    __tablename__ = "llm_budget_reservation"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    # index: обе операции над таблицей фильтруют живые резервы по этому полю.
    reserved_until: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
