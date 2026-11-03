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

Тот же довод — у контракта 5 «Module internals are private, entry via api.py»
(#328): `forbidden_modules` — ручной список внутренних файлов доменных модулей,
и файл без записи импортировался снаружи при зелёном `lint-imports` (к #328 таких
набралось десять). Тесты ниже сверяют список с каталогами `modules/` в обе стороны
и проверяют, что каждый доменный модуль стоит в `source_modules` (сосед не лезет
в чужие внутренности, FOUNDATION §5.1).

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
_MODULES_PACKAGE_DIR = _REPO_ROOT / "src" / "hospitality" / "modules"
_PYPROJECT_PATH = _REPO_ROOT / "pyproject.toml"
_MODULES_PREFIX = "hospitality.modules."

# Подмодули доменного модуля, которые НЕ внутренности, — единственные законные
# отсутствующие в forbidden_modules контракта 5 (явное исключение вместо
# молчаливого отсутствия, #328):
#   api   — публичный вход модуля (§5.2); ради него контракт и существует.
#   tests — тесты модуля, их не импортирует никто (комментарий контракта 5).
# Остальное в modules/<модуль>/ — внутренности. Файлу, который вправе быть
# открытым, место здесь — с обоснованием, а не вне списка.
_NOT_INTERNAL_SUBMODULES = frozenset({"api", "tests"})

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


def _load_module_internals_contract() -> dict[str, Any]:
    """Контракт 5 «Module internals are private, entry via api.py».

    Признак — запрет подмодулей доменных модулей (`hospitality.modules.<модуль>.<файл>`),
    а не имя: контракт 3 запрещает `hospitality.modules` целиком, без точки после.
    """
    pyproject: dict[str, Any] = tomllib.loads(_PYPROJECT_PATH.read_text(encoding="utf-8"))
    contracts: list[dict[str, Any]] = pyproject["tool"]["importlinter"]["contracts"]

    matching = [
        contract
        for contract in contracts
        if contract.get("type") == "forbidden"
        and any(name.startswith(_MODULES_PREFIX) for name in contract.get("forbidden_modules", []))
    ]
    assert len(matching) == 1, (
        "Ожидался ровно один forbidden-контракт import-linter, запрещающий подмодули "
        f"доменных модулей (контракт 5); найдено: {len(matching)}. "
        "Проверьте [tool.importlinter] в pyproject.toml."
    )
    return matching[0]


def _discover_domain_modules() -> list[str]:
    """Имена доменных модулей — пакетов первого уровня под hospitality.modules."""
    return sorted(
        module.name for module in pkgutil.iter_modules([str(_MODULES_PACKAGE_DIR)]) if module.ispkg
    )


def _discover_module_internals() -> set[str]:
    """Полные имена внутренних подмодулей всех доменных модулей (по каталогам)."""
    return {
        f"{_MODULES_PREFIX}{domain}.{submodule.name}"
        for domain in _discover_domain_modules()
        for submodule in pkgutil.iter_modules([str(_MODULES_PACKAGE_DIR / domain)])
        if submodule.name not in _NOT_INTERNAL_SUBMODULES
    }


def test_every_module_internal_is_forbidden_by_module_internals_contract() -> None:
    """Список forbidden_modules контракта 5 совпадает с внутренностями modules/ (#328).

    Невписанный файл импортировался снаружи при зелёном lint-imports; запись
    об удалённом или переименованном файле import-linter молча пропускает.
    """
    discovered = _discover_module_internals()
    # Защита от «пустого» открытия: сломанный путь дал бы зелёный тест впустую.
    assert f"{_MODULES_PREFIX}requests.service" in discovered, (
        f"pkgutil не нашёл requests/service.py в {_MODULES_PACKAGE_DIR} — проверьте путь "
        "до пакета hospitality.modules."
    )

    forbidden = set(_load_module_internals_contract()["forbidden_modules"])
    missing = sorted(discovered - forbidden)
    stale = sorted(forbidden - discovered)

    assert not missing, (
        "Контракт 5 import-linter «Module internals are private, entry via api.py» не "
        f"закрывает внутренние файлы модулей: {missing}. Добавьте каждый в "
        "forbidden_modules (pyproject.toml): снаружи модуль виден только через api.py. "
        "Если файл вправе быть открытым — внесите его имя в _NOT_INTERNAL_SUBMODULES "
        "с обоснованием."
    )
    assert not stale, (
        "В forbidden_modules контракта 5 есть записи без файла в modules/: "
        f"{stale}. Файл удалён или переименован — уберите или поправьте запись "
        "(pyproject.toml): import-linter такие записи пропускает молча."
    )


def test_every_domain_module_is_a_source_of_module_internals_contract() -> None:
    """Каждый доменный модуль стоит в source_modules контракта 5 (§5.1, #328).

    Сосед-модуль входит в чужой модуль только через api.py. Свои внутренности
    модулю доступны: пару «модуль → его же подмодуль» import-linter пропускает
    как перекрытие source и forbidden.
    """
    domain_modules = _discover_domain_modules()
    assert "requests" in domain_modules, (
        f"pkgutil не нашёл модуль requests в {_MODULES_PACKAGE_DIR} — проверьте путь "
        "до пакета hospitality.modules."
    )

    source_modules = set(_load_module_internals_contract()["source_modules"])
    missing = [name for name in domain_modules if f"{_MODULES_PREFIX}{name}" not in source_modules]

    assert not missing, (
        "Контракт 5 import-linter «Module internals are private, entry via api.py» не "
        f"запрещает соседям лезть во внутренности модулей: {missing} нет в "
        "source_modules (pyproject.toml). Добавьте каждый как "
        f"'{_MODULES_PREFIX}<модуль>'."
    )
