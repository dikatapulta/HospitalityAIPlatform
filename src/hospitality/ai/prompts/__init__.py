"""Промпты как код (Task 0015, FOUNDATION §7.5).

Промпты — версионируемые файлы в репозитории (`<name>_v<N>.md`); версия — в
имени. Изменение промпта проходит тот же процесс, что изменение кода, и
покрывается evals (§7.7). `compute_prompt_hash` в gateway фиксирует фактический
текст в журнале каждого вызова.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent


@lru_cache
def load_prompt(name: str) -> str:
    """Прочитать системный промпт по имени версии (например, `concierge_v1`)."""
    return (_PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
