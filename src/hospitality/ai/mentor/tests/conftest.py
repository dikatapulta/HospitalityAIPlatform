"""Фикстуры тестов `ai/mentor` (spec 0048 §11): отель с сотрудником и включённое
назначение `mentor`. Инфраструктура реимпортируется из `tests/conftest.py` (канон).

F811 отключён на файл: фикстура-параметр обязана называться как реимпортированная
фикстура — так pytest связывает их.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from hospitality.ai.gateway.api import MockEmbeddingProvider, MockLlmProvider
from hospitality.ai.mentor.common import MentorProviders
from hospitality.modules.mentor.api import CodexFile
from hospitality.platform.models import Tenant, User
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from tests.conftest import (  # noqa: F401  (реимпорт общих фикстур для pytest)
    _clean_log_context,
    _isolated_event_subscribers,
    canonical_database,
    migrated_database_name,
)

LONG = "Гостя у стойки приветствуют в течение десяти секунд, даже если заняты звонком. " * 4

CODEX = f"""Название: Кодекс отеля «Тест»
# Общая часть
## Раздел 1. Общие положения
### Глава 1. О кодексе
#### Статья 1. Цель кодекса
1. Кодекс описывает стандарты работы.
Источник: проба.
# Особенная часть
## Раздел 2. Служба приёма
### Глава 2. Администратор
#### Статья 2. Встреча гостя у стойки
1. {LONG}
Источник: стандарт службы приёма.
#### Статья 3. Проверка брони
1. Уточните фамилию брони и проверьте документ.
Источник: стандарт службы приёма.
"""


def codex_files(text: str = CODEX) -> list[CodexFile]:
    return [CodexFile(name="10_kodeks.md", text=text)]


def providers(*, answer: str = "Ответ наставника.", summary: str = "Резюме.") -> MentorProviders:
    """Fake-адаптеры шлюза: поток — `answer` по словам, резюме — `summary`, векторы — sha256."""
    return MentorProviders(
        chat=MockLlmProvider(text=summary),
        streaming=MockLlmProvider(text=answer),
        embeddings=MockEmbeddingProvider(),
    )


@pytest.fixture
def mentor_enabled() -> Iterator[None]:
    """Назначение `mentor` включено (канон `ai/gateway/tests/test_embeddings.py`)."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("LLM_MODEL_MENTOR", "gpt-6-luna")
        patch.setenv("LLM_EMBEDDING_MODEL", "text-embedding-3-small")
        get_settings.cache_clear()
        yield
    get_settings.cache_clear()


@pytest.fixture
async def hotel(canonical_database: None) -> tuple[uuid.UUID, uuid.UUID]:
    """`(tenant_id, user_id)` — отель и сотрудник в нём."""
    async with platform_session_scope() as session:
        tenant = Tenant(slug="hotel-a", name="Hotel A")
        user = User(display_name="Анна")
        session.add_all([tenant, user])
        await session.flush()
        return tenant.id, user.id
