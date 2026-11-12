"""Номера и срок хранения (spec 0077 §3.2, §8, §12 PR 3): номера без дыр и дублей при
гонке (чередование — по `pg_locks`), истёкшие записи и брошенные образцы удаляются."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text, update

from hospitality.modules.memos import api as memos_api
from hospitality.modules.memos.models import Memo, MemoKind, MemoSignature, MemoSignatureSample
from hospitality.modules.memos.service import next_number
from hospitality.modules.memos.tests.conftest import STROKES, Hotel, configure, draft, sent_memo
from hospitality.platform.config_cache import clear_tenant_config_cache
from hospitality.platform.models import Tenant
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.tenancy import tenant_context

YEAR = 2026


async def _wait_until_a_backend_blocks(*, timeout_seconds: float = 5.0) -> bool:
    """Дождаться, пока другое соединение встанет на блокировку (канон
    `tests/test_tenant_config.py`): без этого чередование выпадало бы случайно."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        async with platform_session_scope() as session:
            blocked = await session.scalar(
                text(
                    "SELECT count(*) FROM pg_locks lock_row "
                    "JOIN pg_stat_activity backend ON backend.pid = lock_row.pid "
                    "WHERE NOT lock_row.granted AND backend.datname = current_database()"
                )
            )
        if blocked:
            return True
        await asyncio.sleep(0.05)
    return False


async def _number(tenant_id: uuid.UUID) -> str:
    async with session_scope() as session:
        return await next_number(session, tenant_id, MemoKind.MEMO, YEAR)


class _RollbackError(Exception):
    pass


@pytest.mark.parametrize("first_commits", [True, False])
async def test_numbers_have_no_gaps_or_duplicates_under_race(
    hotel: Hotel, first_commits: bool
) -> None:
    """Вторая отправка ждёт счётчик первой: коммит — следующий номер, откат — тот же."""
    with tenant_context(hotel.tenant_id):
        second: asyncio.Task[str] | None = None
        try:
            async with session_scope() as session:
                first = await next_number(session, hotel.tenant_id, MemoKind.MEMO, YEAR)
                second = asyncio.create_task(_number(hotel.tenant_id))
                assert await _wait_until_a_backend_blocks()
                if not first_commits:
                    raise _RollbackError
        except _RollbackError:
            pass
        assert second is not None
        result = await second
        third = await _number(hotel.tenant_id)
        other_kind = await next_number_for(hotel.tenant_id, MemoKind.EXPLANATION)
    assert first == f"СЗ-{YEAR}/1"
    if first_commits:
        assert (result, third) == (f"СЗ-{YEAR}/2", f"СЗ-{YEAR}/3")
    else:
        assert (result, third) == (f"СЗ-{YEAR}/1", f"СЗ-{YEAR}/2")
    assert other_kind == f"ОБ-{YEAR}/1"


async def next_number_for(tenant_id: uuid.UUID, kind: MemoKind) -> str:
    async with session_scope() as session:
        return await next_number(session, tenant_id, kind, YEAR)


async def _age(hotel: Hotel, memo_id: uuid.UUID, *, days: int, sent: bool) -> None:
    moment = utc_now() - timedelta(days=days)
    values = {"sent_at": moment} if sent else {"updated_at": moment}
    with tenant_context(hotel.tenant_id):
        async with session_scope() as session:
            await session.execute(update(Memo).where(Memo.id == memo_id).values(**values))


async def _memo_ids(hotel: Hotel) -> set[uuid.UUID]:
    with tenant_context(hotel.tenant_id):
        async with session_scope() as session:
            return set(await session.scalars(select(Memo.id)))


async def _expire(hotel: Hotel) -> int:
    with tenant_context(hotel.tenant_id):
        return await memos_api.delete_expired_memos(drafts_before=utc_now() - timedelta(days=90))


async def test_retention_deletes_old_drafts_and_expired_memos(hotel: Hotel) -> None:
    """Черновик — 90 дней с правки; отправленная — срок отеля от отправки, вместе с листом.
    Погашенный образец без ссылок уходит, на который ссылается лист — остаётся."""
    await configure(hotel.tenant_id, memo_retention_days=365)
    with tenant_context(hotel.tenant_id):
        old_draft = await memos_api.create_draft(hotel.author, draft(hotel))
        fresh_draft = await memos_api.create_draft(hotel.author, draft(hotel))
    old_sent = await sent_memo(hotel, signers=())
    fresh_sent = await sent_memo(hotel, signers=())
    await _age(hotel, old_draft, days=91, sent=False)
    await _age(hotel, old_sent, days=366, sent=True)
    await _age(hotel, fresh_sent, days=364, sent=True)
    with tenant_context(hotel.tenant_id):
        # Новый образец автора гасит прежний — на него ссылается лист живой записки.
        await memos_api.set_signature_sample(hotel.author, STROKES)
        # Образец подписанта не подписал ничего: погашенный, он больше не нужен.
        await memos_api.revoke_signature_sample(hotel.signer_b.user_id)

    assert await _expire(hotel) == 2

    assert await _memo_ids(hotel) == {fresh_draft, fresh_sent}
    with tenant_context(hotel.tenant_id):
        async with session_scope() as session:
            orphan_rows = await session.scalars(
                select(MemoSignature.id).where(MemoSignature.memo_id == old_sent)
            )
            assert list(orphan_rows) == []
            samples = await session.execute(
                select(MemoSignatureSample.user_id, MemoSignatureSample.revoked_at.is_(None))
            )
            by_user: dict[uuid.UUID, list[bool]] = {}
            for user_id, current in samples:
                by_user.setdefault(user_id, []).append(current)
    assert sorted(by_user[hotel.author.user_id]) == [False, True]
    assert hotel.signer_b.user_id not in by_user
    assert await _expire(hotel) == 0


async def test_retention_without_config_keeps_sent_memos(hotel: Hotel) -> None:
    """Конфиг не читается — срок отправленных неизвестен: удаляются только черновики."""
    old_sent = await sent_memo(hotel, signers=())
    await _age(hotel, old_sent, days=5000, sent=True)
    async with platform_session_scope() as session:
        await session.execute(
            update(Tenant).where(Tenant.id == hotel.tenant_id).values(config=None)
        )
    # Правка колонки мимо `store_tenant_config` — мимо и сброса кэша конфига (#168).
    clear_tenant_config_cache()

    assert await _expire(hotel) == 0
    assert await _memo_ids(hotel) == {old_sent}
