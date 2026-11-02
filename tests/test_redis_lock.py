"""Замок по ключу в Redis (issue #91, `shared/redis_lock.py`).

Живой Redis не нужен: замок проверяется на `FakeRateLimitRedis` (протокол
`LockRedis`) — CI гоняет юнит-тесты без сервиса Redis, как у rate-limit.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from hospitality.shared.logging import configure_logging
from hospitality.shared.redis_lock import hold_redis_lock
from tests.conftest import FakeRateLimitRedis
from tests.test_logging import find_record, read_log_records

GUARD_SECONDS = 5.0


async def _wait_for(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(GUARD_SECONDS):
        while not condition():
            await asyncio.sleep(0.01)


async def test_second_holder_waits_for_the_first() -> None:
    """Второй блок того же ключа начинается только после конца первого."""
    client = FakeRateLimitRedis()
    events: list[str] = []
    first_may_finish = asyncio.Event()

    async def first() -> None:
        async with hold_redis_lock("t", "k", ttl_seconds=60, wait_seconds=5, client=client) as held:
            assert held
            events.append("first:start")
            await first_may_finish.wait()
            events.append("first:end")

    async def second() -> None:
        async with hold_redis_lock("t", "k", ttl_seconds=60, wait_seconds=5, client=client) as held:
            assert held
            events.append("second:start")

    first_task = asyncio.create_task(first())
    await _wait_for(lambda: events == ["first:start"])
    second_task = asyncio.create_task(second())
    await _wait_for(lambda: client.lock_rejections > 0)  # второй ждёт, а не идёт
    first_may_finish.set()
    async with asyncio.timeout(GUARD_SECONDS):
        await asyncio.gather(first_task, second_task)

    assert events == ["first:start", "first:end", "second:start"]
    assert client.locks == {}


async def test_different_keys_do_not_wait() -> None:
    client = FakeRateLimitRedis()
    async with (
        hold_redis_lock("t", "a", ttl_seconds=60, wait_seconds=5, client=client) as a,
        hold_redis_lock("t", "b", ttl_seconds=60, wait_seconds=5, client=client) as b,
    ):
        assert a and b
    assert client.lock_rejections == 0


async def test_lock_is_released_when_the_block_fails() -> None:
    client = FakeRateLimitRedis()
    with pytest.raises(RuntimeError):
        async with hold_redis_lock("t", "k", ttl_seconds=60, wait_seconds=5, client=client):
            raise RuntimeError("boom")
    assert client.locks == {}


async def test_wait_has_a_ceiling(capsys: pytest.CaptureFixture[str]) -> None:
    """Чужой замок не отпускают — после `wait_seconds` блок идёт без замка."""
    configure_logging()
    client = FakeRateLimitRedis()
    client.locks["lock:t:k"] = "someone-else"
    async with asyncio.timeout(GUARD_SECONDS):
        async with hold_redis_lock("t", "k", ttl_seconds=60, wait_seconds=0.3, client=client) as h:
            assert not h
    assert client.locks == {"lock:t:k": "someone-else"}  # чужой замок не тронут
    record = find_record(read_log_records(capsys), "redis_lock_wait_exceeded")
    assert record["level"] == "warning"


async def test_backend_down_fails_open(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    client = FakeRateLimitRedis()
    client.fail = True
    ran = False
    async with hold_redis_lock("t", "k", ttl_seconds=60, wait_seconds=5, client=client) as held:
        ran = True
    assert ran and not held
    record = find_record(read_log_records(capsys), "redis_lock_backend_unavailable")
    assert record["level"] == "warning"


async def test_expired_lock_is_not_released_from_its_new_owner(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Блок пережил TTL, и замок взял другой: конец блока чужой замок не снимает."""
    configure_logging()
    client = FakeRateLimitRedis()
    async with hold_redis_lock("t", "k", ttl_seconds=60, wait_seconds=5, client=client):
        client.locks["lock:t:k"] = "next-owner"  # TTL истёк, следующий взял замок
    assert client.locks == {"lock:t:k": "next-owner"}
    record = find_record(read_log_records(capsys), "redis_lock_expired")
    assert record["level"] == "warning"


async def test_default_client_comes_from_the_redis_canon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Клиент — из `ratelimit.create_redis_client`: его подмена в тестах ходов
    достаёт и замок, иначе CI без Redis шёл бы мимо замка с WARNING."""
    client = FakeRateLimitRedis()
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: client)
    async with hold_redis_lock("t", "k", ttl_seconds=60, wait_seconds=5) as held:
        assert held
        assert client.locks.keys() == {"lock:t:k"}
    assert client.locks == {}


async def test_release_failure_does_not_break_the_block(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Redis упал к концу блока: дело уже сделано, замок снимет TTL — без исключения."""
    configure_logging()
    client = FakeRateLimitRedis()
    async with hold_redis_lock("t", "k", ttl_seconds=60, wait_seconds=5, client=client) as held:
        assert held
        client.fail = True
    record = find_record(read_log_records(capsys), "redis_lock_release_failed")
    assert record["level"] == "warning"
