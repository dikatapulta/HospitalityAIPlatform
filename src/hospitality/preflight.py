"""Проверки конфигурации до запуска процесса (issue #267).

Единственный список fail-fast проверок старта — и единственное место, куда
добавляется новая (P-12). Зовут его три точки:

- ENTRYPOINT образа (`ops/entrypoint.sh`) — ДО `exec` рабочей команды;
- `app.py` и `worker.py` — на сборке своего composition root'а.

Почему мало одних composition root'ов. `SystemExit` роняет ПРОЦЕСС, в котором
исполняется, а `uvicorn --workers N` (CMD цели `production`) и `uvicorn --reload`
(цель `dev`) держат над рабочим процессом собственного родителя-супервизора:
родитель `SystemExit` ребёнка не наследует. Замер на ветке issue #267 — 8
перезапусков детей за 25 с при живом родителе; контейнер числится живым, и
`docker compose up --wait` ждёт healthcheck до таймаута вместо мгновенного
падения. ENTRYPOINT исполняет проверки в том процессе, который и станет PID 1
контейнера, поэтому провал — это выход контейнера с кодом 1 при любой команде.

Зачем тогда вызовы в composition root'ах остались: вне контейнера (`uvicorn`
руками, `pytest`, разовый скрипт) ENTRYPOINT'а нет, а подняться с негодной
конфигурацией не вправе ни один процесс.
"""

from __future__ import annotations

from hospitality.ai.gateway.api import validate_configured_model
from hospitality.shared.config import get_settings


def run_preflight() -> None:
    """Прогоняет проверки конфигурации; негодная настройка = отказ старта."""
    # Сборка Settings — сама по себе проверка: границы значений стоят на полях
    # (`llm_max_attempts` с `ge=1`, issue #273), и невозможная настройка падает
    # здесь ошибкой pydantic с именем поля и границей — а не глубже в коде на
    # `assert`, то есть 500-кой у гостя. Тот же канон, что у `log_level`
    # (`Literal` вместо `str`, shared/config.py).
    get_settings()
    validate_configured_model()


if __name__ == "__main__":  # pragma: no cover — точка входа процесса
    run_preflight()
