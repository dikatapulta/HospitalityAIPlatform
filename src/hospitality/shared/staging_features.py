"""Staging показывает всё, что есть в `main`, — одной строкой `.env` (#940).

Слово основателя 10.11.2026: «на стейджинге включать все». Выключатель окружения
`STAGING_ALL_FEATURES=true` пишется один раз в `.env` staging; с ним `Settings`:

- читает каждый выключатель раздела `enable_*` как включённый — и тот, что compose
  передал явным `false` из умолчания, — кроме `STAGING_ALL_FEATURES_EXCLUDED`;
- даёт пустой модели раздела `llm_model_*` модель из `STAGING_SECTION_MODELS`,
  если задан ключ её провайдера. Без ключа поле остаётся пустым — раздел без модели
  (ERR-AI-006), а не процесс, который не стартует (`validate_configured_model`).

Флаги тенанта демо-отеля (`*_enabled` в `TenantConfig`) включает сид на деплое
(`tools/seed.py`), с проверкой их зависимостей.

Отказ в безопасную сторону: без строки — прежние умолчания, `false` и пустые модели.
Включение не выводится из имени хоста, `SENTRY_ENVIRONMENT` или slug отеля: гостевая
копия поднимается тем же compose-файлом, её `SENTRY_ENVIRONMENT=pilot` — строка `.env`,
которую можно забыть (#389), и ошибка в определении контура включила бы раздел живым гостям.

Новый раздел подчиняется выключателю сам — по имени поля. Модель нового раздела —
строка в `STAGING_SECTION_MODELS`, иначе красный `tests/test_staging_all_features.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from hospitality.shared.config import Settings

SectionProvider = Literal["anthropic", "openai"]

# Выключатель раздела, который общий выключатель не трогает, — и почему.
STAGING_ALL_FEATURES_EXCLUDED: Final[dict[str, str]] = {
    "enable_handover_manual": (
        "на staging — демо передачи смены (ENABLE_DEMO_HANDOVER), оно главнее ручного бланка"
    ),
}

# Модель раздела на staging и её провайдер — по выбору спеки или рунбука раздела.
# Провайдер записан здесь, а не взят из прайс-листа: ядро не импортирует `ai/`
# (R-5); совпадение с прайс-листом сверяет тест.
STAGING_SECTION_MODELS: Final[dict[str, tuple[str, SectionProvider]]] = {
    "llm_model_mentor": ("gpt-6-luna", "openai"),  # docs/runbooks/mentor.md
    "llm_model_documents": ("claude-sonnet-5", "anthropic"),  # фото — только Anthropic
    "llm_model_showroom": ("claude-sonnet-5", "anthropic"),  # spec 0069 §2.3
    "llm_model_ideas": ("claude-sonnet-5", "anthropic"),  # spec 0071 §7
}


def apply_staging_all_features(settings: Settings) -> None:
    """Включить все разделы в собранных `Settings`, если выключатель стоит."""
    if not settings.staging_all_features:
        return
    for name in type(settings).model_fields:
        if name.startswith("enable_") and name not in STAGING_ALL_FEATURES_EXCLUDED:
            setattr(settings, name, True)
    for name, (model, provider) in STAGING_SECTION_MODELS.items():
        if not getattr(settings, name) and _provider_key(settings, provider):
            setattr(settings, name, model)


def staging_models_without_key(settings: Settings) -> list[str]:
    """Модели разделов, оставшиеся пустыми при выключателе: нет ключа провайдера."""
    if not settings.staging_all_features:
        return []
    return [name for name in STAGING_SECTION_MODELS if not getattr(settings, name)]


def _provider_key(settings: Settings, provider: SectionProvider) -> str:
    return settings.openai_api_key if provider == "openai" else settings.anthropic_api_key
