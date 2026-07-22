"""Мета-тест конфигурации import-linter (H-7, docs-drift, Gate A).

Контракт 4 «LLM providers only via ai/gateway» (`pyproject.toml`) требует, чтобы
каждый подпакет `ai/`, кроме `gateway`, был перечислен в `source_modules`: только
`gateway` легально импортирует SDK провайдера напрямую, все остальные зовут LLM
через него. Требование было лишь комментарием — и модуль `ai/translation.py`
(баг #71) в контракт не попал, пока это не поймал аудит.

Этот тест делает требование машинным: перечисляет реальные подпакеты/модули
`hospitality.ai` (pkgutil по каталогу) и сверяет их с `source_modules` контракта
(tomllib по `pyproject.toml`). Следующий подпакет `ai/` без записи в контракте
уронит CI, а не будет ждать следующего аудита.

Канон теста: строгая типизация, без сети и БД — только чтение файлов репозитория.
"""

from __future__ import annotations

import pkgutil
import tomllib
from pathlib import Path
from typing import Any

# tests/ лежит в корне репозитория — родитель этого файла есть корень.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_AI_PACKAGE_DIR = _REPO_ROOT / "src" / "hospitality" / "ai"
_PYPROJECT_PATH = _REPO_ROOT / "pyproject.toml"

# Подпакеты ai/, законно отсутствующие в source_modules контракта 4:
#   gateway — единственное место, где прямой импорт SDK провайдера легален
#             (ai/gateway/anthropic_provider.py); ради этого контракт и существует.
#   tests   — тестовая обвязка, а не боевой код маршрутизации LLM. Проект уже
#             трактует тест-пакеты как вне граничных контрактов (ср. контракт 5:
#             «tests модуля в forbidden не входят»), а ai/gateway/tests легально
#             импортирует anthropic напрямую. Новый БОЕВОЙ подпакет ai/ сюда не
#             попадает — он обязан быть в контракте.
_EXEMPT_AI_SUBPACKAGES = frozenset({"gateway", "tests"})

# Стабильный идентификатор контракта 4 в pyproject: единственный forbidden-контракт,
# запрещающий прямой импорт SDK LLM-провайдера. Ищем по этому признаку, а не по имени
# (имя — человекочитаемое и может меняться при рефакторинге текста).
_LLM_PROVIDER_SDK = "anthropic"


def _load_llm_gateway_contract_source_modules() -> list[str]:
    """`source_modules` контракта «LLM providers only via ai/gateway»."""
    pyproject: dict[str, Any] = tomllib.loads(_PYPROJECT_PATH.read_text(encoding="utf-8"))
    contracts: list[dict[str, Any]] = pyproject["tool"]["importlinter"]["contracts"]

    matching = [
        contract
        for contract in contracts
        if contract.get("type") == "forbidden"
        and _LLM_PROVIDER_SDK in contract.get("forbidden_modules", [])
    ]
    assert len(matching) == 1, (
        "Ожидался ровно один forbidden-контракт import-linter, запрещающий прямой "
        f"импорт {_LLM_PROVIDER_SDK!r} (контракт 4); найдено: {len(matching)}. "
        "Проверьте [tool.importlinter] в pyproject.toml."
    )
    source_modules: list[str] = matching[0]["source_modules"]
    return source_modules


def _discover_ai_subpackages() -> list[str]:
    """Имена подпакетов/модулей первого уровня под hospitality.ai (по каталогу)."""
    return sorted(module.name for module in pkgutil.iter_modules([str(_AI_PACKAGE_DIR)]))


def test_every_ai_subpackage_is_covered_by_llm_gateway_contract() -> None:
    """Каждый боевой подпакет ai/ (кроме gateway) объявлен в контракте 4.

    H-7: `ai/translation.py` появился с фиксом бага #71, но в source_modules
    контракта его забыли внести. Тест не даёт этому повториться.
    """
    discovered = _discover_ai_subpackages()
    # Защита от «пустого» открытия: сломанный путь дал бы зелёный тест впустую.
    assert "gateway" in discovered, (
        f"pkgutil не нашёл подпакет gateway в {_AI_PACKAGE_DIR} — проверьте путь до "
        "пакета hospitality.ai."
    )

    source_modules = set(_load_llm_gateway_contract_source_modules())
    required = [name for name in discovered if name not in _EXEMPT_AI_SUBPACKAGES]
    missing = [name for name in required if f"hospitality.ai.{name}" not in source_modules]

    assert not missing, (
        "Контракт 4 import-linter «LLM providers only via ai/gateway» не покрывает "
        f"подпакеты ai/: {sorted(missing)}. Добавьте каждый в source_modules "
        "(pyproject.toml): все подпакеты ai/, кроме gateway, зовут LLM только через "
        "gateway. Если подпакет — тестовая обвязка, внесите его в "
        "_EXEMPT_AI_SUBPACKAGES с обоснованием."
    )
