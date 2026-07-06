"""AI Gateway — единственная дверь к LLM-провайдерам (FOUNDATION §7.2).

Пакет-маркер слоя (Task 0003): контракт import-linter'а «LLM providers only via
ai/gateway» действует до появления реализации gateway. Прямые импорты SDK
LLM-провайдеров из любого другого места запрещены (R-5).
"""
