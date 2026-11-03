"""Помощник 3D-витрины — LLM-часть (spec 0069 §2.3, ADR-025).

Композиционный слой над модулем `showroom`: модуль владеет сценой и поиском номеров
и модели не знает (FOUNDATION §5.1); здесь — правила без модели (`rules.py`),
сводка сцены для промпта (`summary.py`) и ход помощника через шлюз с назначением
`showroom` (`navigate.py`, ADR-024). Модель — только через `ai/gateway` (контракт 4).

Вход — `navigate.navigate`, внутри `tenant_context`; маршруты — `channels/showroom/navigate.py`.
"""
