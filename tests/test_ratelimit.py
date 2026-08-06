"""Канонический Redis-счётчик rate-limit (issue #41, spec 0023, план тестов п.1).

Живой Redis не нужен: счётчик проверяется на `FakeRateLimitRedis` (протокол
`RateLimitRedis`) — юнит-тесты CI идут без сервиса Redis. Поведение на живом
Redis покрывает smoke-набор (compose поднимает redis:7).
"""

from __future__ import annotations

from hospitality.shared.ratelimit import consume_rate_limit, peek_rate_limit
from tests.conftest import FakeRateLimitRedis

LIMIT = 3
WINDOW = 600


async def test_allows_up_to_limit_then_rejects() -> None:
    """N событий проходят, (N+1)-е отклонено; first_rejection — только у него."""
    client = FakeRateLimitRedis()
    for expected_count in range(1, LIMIT + 1):
        decision = await consume_rate_limit(
            "test_scope", "tenant:chat", limit=LIMIT, window_seconds=WINDOW, client=client, now=0
        )
        assert decision.allowed
        assert decision.count == expected_count
        assert decision.available

    rejected = await consume_rate_limit(
        "test_scope", "tenant:chat", limit=LIMIT, window_seconds=WINDOW, client=client, now=0
    )
    assert not rejected.allowed
    assert rejected.first_rejection  # момент единственного ответа-отказа

    rejected_again = await consume_rate_limit(
        "test_scope", "tenant:chat", limit=LIMIT, window_seconds=WINDOW, client=client, now=0
    )
    assert not rejected_again.allowed
    assert not rejected_again.first_rejection  # дальше — молча


async def test_window_rollover_resets_count() -> None:
    """Смена окна (bucket в ключе) сама обнуляет счёт — EXPIRE лишь гигиена."""
    client = FakeRateLimitRedis()
    for _ in range(LIMIT + 1):
        await consume_rate_limit(
            "test_scope", "tenant:chat", limit=LIMIT, window_seconds=WINDOW, client=client, now=0
        )

    next_window = await consume_rate_limit(
        "test_scope", "tenant:chat", limit=LIMIT, window_seconds=WINDOW, client=client, now=WINDOW
    )
    assert next_window.allowed
    assert next_window.count == 1


async def test_scopes_and_keys_are_isolated() -> None:
    """Разные scope и разные key считаются независимо (изоляция тенантов — P-4)."""
    client = FakeRateLimitRedis()
    for _ in range(LIMIT + 1):
        await consume_rate_limit(
            "scope_a", "tenant-a:chat", limit=LIMIT, window_seconds=WINDOW, client=client, now=0
        )

    other_scope = await consume_rate_limit(
        "scope_b", "tenant-a:chat", limit=LIMIT, window_seconds=WINDOW, client=client, now=0
    )
    other_key = await consume_rate_limit(
        "scope_a", "tenant-b:chat", limit=LIMIT, window_seconds=WINDOW, client=client, now=0
    )
    assert other_scope.allowed
    assert other_key.allowed


async def test_unavailable_redis_fails_open() -> None:
    """Redis упал → allowed=True, available=False (spec 0023: отказ хуже перерасхода)."""
    client = FakeRateLimitRedis()
    client.fail = True
    decision = await consume_rate_limit(
        "test_scope", "tenant:chat", limit=LIMIT, window_seconds=WINDOW, client=client, now=0
    )
    assert decision.allowed
    assert not decision.available
    assert not decision.first_rejection  # fail-open не порождает ответ-отказ


async def test_peek_does_not_spend_budget() -> None:
    """Issue #207: проверка бюджета не тратит его — иначе успешный вход в
    кабинет съедал бы попытки, отведённые на подбор пароля."""
    client = FakeRateLimitRedis()
    for _ in range(LIMIT * 2):
        decision = await peek_rate_limit(
            "test_scope", "tenant:chat", limit=LIMIT, window_seconds=WINDOW, client=client, now=0
        )
        assert decision.allowed
        assert decision.count == 0

    assert client.counters == {}  # ни одного INCR


async def test_peek_sees_events_of_consume_and_closes_at_limit() -> None:
    """Пара к consume: тот же ключ и то же окно — счёт общий."""
    client = FakeRateLimitRedis()
    for _ in range(LIMIT):
        await consume_rate_limit(
            "test_scope", "tenant:chat", limit=LIMIT, window_seconds=WINDOW, client=client, now=0
        )

    exhausted = await peek_rate_limit(
        "test_scope", "tenant:chat", limit=LIMIT, window_seconds=WINDOW, client=client, now=0
    )
    assert not exhausted.allowed
    assert exhausted.count == LIMIT

    next_window = await peek_rate_limit(
        "test_scope", "tenant:chat", limit=LIMIT, window_seconds=WINDOW, client=client, now=WINDOW
    )
    assert next_window.allowed  # смена окна обнуляет счёт и для peek


async def test_peek_fails_open_when_redis_is_down() -> None:
    client = FakeRateLimitRedis()
    client.fail = True
    decision = await peek_rate_limit(
        "test_scope", "tenant:chat", limit=LIMIT, window_seconds=WINDOW, client=client, now=0
    )
    assert decision.allowed
    assert not decision.available


async def test_expire_is_set_on_key_creation() -> None:
    """TTL ставится при создании ключа (гигиена: ключи прошлых окон умирают сами)."""
    client = FakeRateLimitRedis()
    await consume_rate_limit(
        "test_scope", "tenant:chat", limit=LIMIT, window_seconds=WINDOW, client=client, now=0
    )
    (redis_key,) = client.counters
    assert redis_key == f"ratelimit:test_scope:{WINDOW}:tenant:chat:0"
    assert client.ttls[redis_key] == WINDOW * 2
