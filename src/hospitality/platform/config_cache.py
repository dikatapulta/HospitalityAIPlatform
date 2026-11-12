"""Кэш чтения конфига тенанта (#168): короткий срок жизни, ключ — `tenant_id`.

Зачем. Конфиг читают на каждый запрос: опрос очереди и сигнала кабинета — раз
в 15 с с каждой открытой вкладки, оркестратор — каждым ходом гостя. Меняется он
раз в недели. Кэш держит конфиг `TENANT_CONFIG_CACHE_TTL_SECONDS` и снимает почти
всё чтение `tenants` из базы, сколько бы вкладок ни было открыто.

Чем пользуется только `config.py`: путь чтения и записи остаётся прежним
(`load_tenant_config` / `store_tenant_config` / `mutate_tenant_config`, P-12).

Что хранится — словарь колонки `tenants.config` (или `None` — онбординг не
завершён), а не модель: `load_tenant_config` валидирует его заново на каждый
вызов и отдаёт новые объекты, поэтому правка возвращённого конфига до кэша
не дойдёт, а коды отказа и WARNING о незнакомых полях остаются прежними.
Тенант, которого нет (ERR-PLATFORM-004), не кэшируется.

Свежесть:
- свой процесс: запись сбрасывает строку кэша сразу и ещё раз после commit своей
  транзакции (`after_commit`) — чтение другой сессии между записью и commit
  видит прежний конфиг, и после commit он в кэше не остаётся. Чтение, начатое
  до сброса, в кэш не ложится (метка сброса `_forgotten_at`);
- та же сессия после записи читает мимо кэша: она видит свою незакоммиченную
  запись, а другим запросам эта запись не видна до commit — и никогда, если
  транзакция откатится;
- другой процесс (воркер, CLI `tools/*`, второй процесс uvicorn) видит правку
  не позже чем через срок жизни. В процессе кабинета запись и чтение — один
  процесс: сохранённая менеджером настройка видна на следующей же странице.

Изоляция тенанта (ADR-003): `tenants` — реестр без RLS, и `load_tenant_config`
читает его строку по id любой сессией. Ключ кэша — тот же id, поэтому кэш
не отдаёт ничего, чего тому же вызову не отдала бы база.
"""

from __future__ import annotations

import copy
import time
import uuid
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

# Срок жизни строки кэша. Несколько секунд уже снимают почти всю нагрузку: при
# 5 с база видит не больше одного чтения конфига отеля на процесс за 5 с, а
# не по одному на каждую вкладку раз в 15 с. Больше — дольше другой процесс
# не видит правку.
TENANT_CONFIG_CACHE_TTL_SECONDS: Final = 5.0

# Ключ `Session.info`: тенанты, чей конфиг эта сессия записала в текущей транзакции.
_WRITTEN_KEY: Final = "hospitality.platform.tenant_configs_written"


@dataclass(frozen=True, slots=True)
class CachedConfig:
    """Строка кэша: словарь колонки `tenants.config` и момент, когда она протухнет."""

    raw: dict[str, Any] | None
    expires_at: float


_entries: dict[uuid.UUID, CachedConfig] = {}
# Когда строку тенанта сбросили в последний раз (`time.monotonic`): чтение,
# начатое раньше, могло вернуть уже заменённый конфиг.
_forgotten_at: dict[uuid.UUID, float] = {}


def lookup(session: AsyncSession, tenant_id: uuid.UUID) -> CachedConfig | None:
    """Свежая строка кэша; `None` — читать базу.

    Сессия, записавшая конфиг этого тенанта в текущей транзакции, кэш не
    читает: свою запись она обязана видеть сразу.
    """
    if _written_in(session, tenant_id):
        return None
    entry = _entries.get(tenant_id)
    if entry is None or entry.expires_at <= time.monotonic():
        return None
    return entry


def read_started() -> float:
    """Метка начала чтения из базы — аргумент `remember`."""
    return time.monotonic()


def remember(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    raw: dict[str, Any] | None,
    *,
    started: float,
) -> None:
    """Положить в кэш словарь, прочитанный из базы с момента `started`.

    Не кладёт, если конфиг сбросили после начала чтения (оно могло вернуть
    заменённый конфиг) или если сессия сама писала его в этой транзакции
    (незакоммиченное не уходит другим запросам). Копия — чтобы правка словаря
    в объекте сессии не дошла до кэша.
    """
    if _written_in(session, tenant_id) or _forgotten_at.get(tenant_id, float("-inf")) >= started:
        return
    _entries[tenant_id] = CachedConfig(
        raw=copy.deepcopy(raw),
        expires_at=time.monotonic() + TENANT_CONFIG_CACHE_TTL_SECONDS,
    )


def forget_on_write(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Запись конфига: сбросить строку сейчас и ещё раз после commit сессии."""
    _forget(tenant_id)
    written: set[uuid.UUID] = session.info.setdefault(_WRITTEN_KEY, set())
    written.add(tenant_id)
    sync_session = session.sync_session
    if not event.contains(sync_session, "after_commit", _forget_written_after_commit):
        event.listen(sync_session, "after_commit", _forget_written_after_commit)


def clear_tenant_config_cache() -> None:
    """Забыть всё. Для тестов: свежая база на тест — новый реестр тенантов
    (`canonical_database`), а правка колонки мимо `store_tenant_config` —
    мимо и сброса кэша."""
    _entries.clear()
    _forgotten_at.clear()


def _written_in(session: AsyncSession, tenant_id: uuid.UUID) -> bool:
    written: set[uuid.UUID] | None = session.info.get(_WRITTEN_KEY)
    return written is not None and tenant_id in written


def _forget_written_after_commit(sync_session: Session) -> None:
    written: set[uuid.UUID] = sync_session.info.pop(_WRITTEN_KEY, set())
    for tenant_id in written:
        _forget(tenant_id)


def _forget(tenant_id: uuid.UUID) -> None:
    _entries.pop(tenant_id, None)
    _forgotten_at[tenant_id] = time.monotonic()
