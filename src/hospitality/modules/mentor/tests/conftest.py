"""Фикстуры тестов модуля mentor (spec 0048) — канон `modules/requests/tests/conftest.py`:
общие DB-фикстуры реимпортируются из `tests/conftest.py`.

F811 отключён на файл: фикстура-параметр (`canonical_database` в `two_tenants`)
обязана называться как реимпортированная фикстура — так pytest связывает их.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid

import pytest

from hospitality.modules.mentor.api import ARTICLE_EMBEDDING_DIMENSIONS, CodexFile
from hospitality.platform.models import Tenant, User
from hospitality.shared.db import platform_session_scope
from tests.conftest import (  # noqa: F401  (реимпорт общих фикстур для pytest)
    _clean_log_context,
    _isolated_event_subscribers,
    canonical_database,
    migrated_database_name,
)

HEAD = "Название: Кодекс отеля «Тест»\nСтатус: Проект\n"

# Образец разметки — из тестов «Наставника» (`tests/test_kodeks.py`), статьи 2-1 и 3
# удлинены до тренировочных (от 250 знаков).
LONG = "Гость должен получить ответ в течение десяти минут, а если ответа нет — извинение. " * 4
BODY = f"""# Общая часть
## Раздел 1. Общие положения
### Глава 1. О кодексе
> Основание: проба.
#### Статья 1. Первая
1. Пункт:
1) подпункт;
2) ещё подпункт.
2. Второй пункт.
Источник: проба.
# Особенная часть
## Раздел 2. Служба
### Глава 2. Консьерж
#### Статья 2. Вторая
Абзац без номера.
Источник: проба.
#### Статья 2-1. Вставка
1. {LONG}
Источник: проба.
### Глава 3. Портье
#### Статья 3. Третья
1. {LONG}
Источник: стандарт портье.
"""


def codex_files(body: str = BODY, head: str = HEAD) -> list[CodexFile]:
    return [CodexFile(name="10_body.md", text=body), CodexFile(name="00_head.md", text=head)]


def unit_vector(index: int) -> list[float]:
    """Единичный вектор по оси `index`: ближайший к нему — он сам."""
    vector = [0.0] * ARTICLE_EMBEDDING_DIMENSIONS
    vector[index] = 1.0
    return vector


@pytest.fixture
async def two_tenants(canonical_database: None) -> tuple[uuid.UUID, uuid.UUID]:
    """Два тенанта в реестре — «Hotel A» и «Hotel B» (канон test_tenant_isolation)."""
    async with platform_session_scope() as session:
        tenant_a = Tenant(slug="hotel-a", name="Hotel A")
        tenant_b = Tenant(slug="hotel-b", name="Hotel B")
        session.add_all([tenant_a, tenant_b])
        await session.flush()
        return (tenant_a.id, tenant_b.id)


async def make_user(display_name: str) -> uuid.UUID:
    """Платформенный User: `user_id` — FK на `users`, случайный uuid БД не пропустит."""
    async with platform_session_scope() as session:
        user = User(display_name=display_name)
        session.add(user)
        await session.flush()
        return user.id
