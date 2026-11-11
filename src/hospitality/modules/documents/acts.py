"""Черновик АВР по кнопке бухгалтера (spec 0086, issue #953).

Акт формируется только для строки «Проверено» (spec 0049 Р-5), не обезличенной и
не возвращённой гостю, и только вызовом из кабинета (NG-4, P-9): подписчика,
джобы и инструмента ИИ, создающих акт, нет. В акт ложится снимок — реквизиты
строки, кто их проверил, исполнитель из конфига тенанта, номер, дата, строки и
суммы; правки акта нет, ошибка — новый акт (Р-2, Р-5). Файл собирает адаптер
порта `AccountingPort` из снимка при каждом скачивании (Р-7).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Final, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.documents.codec import (
    ActSnapshot,
    decrypt_act,
    decrypt_requisites,
    encrypt_act,
)
from hospitality.modules.documents.journal import journal_record
from hospitality.modules.documents.models import ActRequest, WorkCompletionAct
from hospitality.modules.documents.schemas import (
    ActingUser,
    WorkCompletionActCreate,
    WorkCompletionActRead,
    line_cost,
)
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import ActExecutor, load_tenant_config
from hospitality.platform.staff_actions import StaffAction, record_staff_action
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_work_completion_act
from hospitality.shared.ports.accounting import (
    ActDocument,
    ActFileFormat,
    ActLine,
    ActParty,
    ExportFile,
    get_accounting,
)
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_DOCUMENTS_NOT_FOUND: Final = "ERR-DOCUMENTS-004"
ERR_DOCUMENTS_NOT_CHECKED: Final = "ERR-DOCUMENTS-007"
ERR_DOCUMENTS_NO_EXECUTOR: Final = "ERR-DOCUMENTS-008"


async def create_work_completion_act(
    act_request_id: uuid.UUID, data: WorkCompletionActCreate, *, acting_user: ActingUser
) -> WorkCompletionActRead:
    """Сформировать черновик АВР из проверенных реквизитов запроса (spec 0086 Р-2).

    Нет исполнителя в конфиге — ERR-DOCUMENTS-008; запроса или его заявки нет,
    запрос обезличен — 004; строка не «Проверено» или заявка отменена (запрос
    возвращён гостю) — 007. Журнал — той же сессией, что акт (spec 0041 §2.1).
    """
    executor = await _executor()
    journal = await journal_record(act_request_id, StaffAction.WORK_COMPLETION_ACT_CREATED)
    if journal is None or journal.request_id is None:
        raise _rejected(ERR_DOCUMENTS_NOT_FOUND, "Запрос не найден.", 404)
    requests = await requests_api.list_requests_by_ids([journal.request_id])
    returned = not requests or requests[0].status is requests_api.RequestStatus.CANCELLED
    async with session_scope() as session:
        row: ActRequest | None = await session.scalar(
            select(ActRequest).where(ActRequest.id == act_request_id).with_for_update()
        )
        if row is None or row.anonymized_at is not None:
            raise _rejected(ERR_DOCUMENTS_NOT_FOUND, "Запрос не найден.", 404)
        if row.checked_at is None or returned:
            raise _rejected(ERR_DOCUMENTS_NOT_CHECKED, "Реквизиты не подтверждены.", 409)
        requisites = decrypt_requisites(row).values
        document = _document(
            data, requisites.company_name, requisites.bin_iin, requisites.address, executor
        )
        act = WorkCompletionAct(
            id=uuid.uuid4(),
            tenant_id=current_tenant_id(),
            act_request_id=row.id,
            number=document.number,
            composed_on=document.composed_on,
            total=document.total,
            created_by_user_id=acting_user.user_id,
            created_by_display_name=acting_user.display_name,
            created_at=utc_now(),
        )
        act.content_ciphertext = encrypt_act(
            act,
            ActSnapshot(
                document=document,
                requisites=requisites,
                checked_at=row.checked_at,
                checked_by_display_name=row.checked_by_display_name,
            ),
        )
        session.add(act)
        record_staff_action(session, acting_user, journal)
        await session.flush()
        result = _read(act)
    # Значений в логе нет: какой акт, к какому запросу, сколько строк и кто.
    logger.info(
        "work_completion_act_created",
        act_id=str(result.id),
        act_request_id=str(act_request_id),
        lines=len(document.lines),
        user_id=str(acting_user.user_id),
    )
    record_work_completion_act("created")
    return result


async def list_work_completion_acts(
    act_request_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, list[WorkCompletionActRead]]:
    """Акты строк страницы по id запроса, новые сверху; у запроса без актов ключа нет."""
    if not act_request_ids:
        return {}
    async with session_scope() as session:
        acts = await session.scalars(
            select(WorkCompletionAct)
            .where(WorkCompletionAct.act_request_id.in_(act_request_ids))
            .order_by(WorkCompletionAct.created_at.desc(), WorkCompletionAct.id)
        )
        result: dict[uuid.UUID, list[WorkCompletionActRead]] = {}
        for act in acts:
            result.setdefault(act.act_request_id, []).append(_read(act))
    return result


async def render_work_completion_act(
    act_id: uuid.UUID, file_format: ActFileFormat, *, acting_user: ActingUser
) -> ExportFile:
    """Файл акта из снимка; акта нет у отеля или он обезличен — ERR-DOCUMENTS-004.

    Ничего не меняет — журнал не пишется (Р-8), след — лог и метрика.
    """
    async with session_scope() as session:
        act = await session.get(WorkCompletionAct, act_id)
        snapshot = decrypt_act(act) if act is not None else None
    if snapshot is None:
        raise _rejected(ERR_DOCUMENTS_NOT_FOUND, "Акт не найден.", 404)
    accounting = get_accounting()
    rendered = await accounting.render_act(snapshot.document, file_format=file_format)
    logger.info(
        "work_completion_act_rendered",
        act_id=str(act_id),
        file_format=file_format.value,
        backend=accounting.backend,
        user_id=str(acting_user.user_id),
    )
    record_work_completion_act("rendered")
    return rendered


async def anonymize_acts_of_requests(session: AsyncSession, *, created_before: datetime) -> int:
    """Обезличить акты запросов старше срока — тем же часом, что сами запросы (Р-6).

    Зовёт `processing.anonymize_expired_act_requests` той же сессией: снимок
    акта несёт те же ПД, что запрос, и не переживает его.
    """
    expired = select(ActRequest.id).where(ActRequest.created_at < created_before)
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(WorkCompletionAct)
            .where(
                WorkCompletionAct.act_request_id.in_(expired),
                WorkCompletionAct.anonymized_at.is_(None),
            )
            .values(content_ciphertext=None, created_by_display_name=None, anonymized_at=utc_now())
        ),
    )
    return result.rowcount or 0


def _document(
    data: WorkCompletionActCreate,
    company_name: str,
    bin_iin: str,
    address: str,
    executor: ActExecutor,
) -> ActDocument:
    """Снимок файла: стоимости, «Итого» и количество в «Итого» посчитаны здесь (Р-3)."""
    lines = tuple(
        ActLine(
            service=line.service,
            service_date=line.service_date,
            unit=line.unit,
            quantity=line.quantity,
            price=line.price.quantize(Decimal("0.01")),
            cost=line_cost(line.quantity, line.price),
        )
        for line in data.lines
    )
    single_unit = len({line.unit for line in lines}) == 1
    return ActDocument(
        number=data.number,
        composed_on=data.composed_on,
        contract=data.contract,
        customer=ActParty(name=company_name, bin_iin=bin_iin, address=address),
        executor=ActParty(
            name=executor.name,
            bin_iin=executor.bin_iin,
            address=executor.address,
            contact=executor.contact,
        ),
        lines=lines,
        quantity_total=sum((line.quantity for line in lines), Decimal(0)) if single_unit else None,
        total=sum((line.cost for line in lines), Decimal(0)),
    )


async def _executor() -> ActExecutor:
    async with platform_session_scope() as session:
        config = await load_tenant_config(session, current_tenant_id())
    if config.act_executor is None:
        raise _rejected(ERR_DOCUMENTS_NO_EXECUTOR, "Реквизиты отеля для АВР не заданы.", 409)
    return config.act_executor


def _read(act: WorkCompletionAct) -> WorkCompletionActRead:
    return WorkCompletionActRead(
        id=act.id,
        act_request_id=act.act_request_id,
        number=act.number,
        composed_on=act.composed_on,
        total=act.total,
        created_by_display_name=act.created_by_display_name,
        created_at=act.created_at,
        anonymized=act.anonymized_at is not None,
    )


def _rejected(code: str, message: str, status_code: int) -> AppError:
    return AppError(code=code, message=message, status_code=status_code)
