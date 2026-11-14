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

Строка — необходимое условие, но не достаточное (#1053): выключатель действует, только
когда контур по всем трём признакам staging (`staging_contour_refusals`, те же признаки
у демо-инструментов). Строка, перенесённая в `.env` копии отеля, разделы не трогает —
процесс пишет ошибку `ERR-PLATFORM-012` и стартует с прежними умолчаниями.

Новый раздел подчиняется выключателю сам — по имени поля. Модель нового раздела —
строка в `STAGING_SECTION_MODELS`, иначе красный `tests/test_staging_all_features.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import urlsplit

from hospitality.shared.logging import get_logger

if TYPE_CHECKING:
    from hospitality.shared.config import Settings

logger = get_logger(module=__name__)

SectionProvider = Literal["anthropic", "openai"]

# Выключатель стоит на контуре, который не staging, — разделы не тронуты (#1053).
STAGING_ALL_FEATURES_REFUSED_ERROR_CODE: Final = "ERR-PLATFORM-012"

STAGING_ENVIRONMENT: Final = "staging"
# Отель staging — демо-отель. Записан здесь, а не взят из `platform/seed.py`: ядро
# `shared` не импортирует `platform` (R-5); совпадение сверяет тест.
STAGING_TENANT_SLUG: Final = "demo-hotel"

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
    "llm_model_memos": ("claude-sonnet-5", "anthropic"),  # spec 0077 §7
}


def staging_contour_refusals(settings: Settings) -> list[str]:
    """Почему этот контур — не staging; пустой список — staging по всем трём признакам.

    У гостевой копии отеля `SENTRY_ENVIRONMENT` тоже `staging`, если строку `pilot`
    в её `.env` забыли (#389), поэтому копию отсекают отель и адрес контура.
    """
    refusals: list[str] = []
    if settings.sentry_environment != STAGING_ENVIRONMENT:
        refusals.append(
            f"SENTRY_ENVIRONMENT={settings.sentry_environment!r}, а нужен {STAGING_ENVIRONMENT!r}"
        )
    if settings.telegram_tenant_slug != STAGING_TENANT_SLUG:
        # Значение не печатается: код отеля пилота в репозиторий и логи не попадает.
        refusals.append(
            f"отель этого контура (TELEGRAM_TENANT_SLUG) — не {STAGING_TENANT_SLUG}: "
            "похоже на копию отеля, туда демо нельзя"
        )
    host = urlsplit(settings.public_base_url).hostname or ""
    if not host.startswith(f"{STAGING_ENVIRONMENT}."):
        refusals.append(f"адрес контура (PUBLIC_BASE_URL) {host!r} — не staging.…")
    return refusals


def apply_staging_all_features(settings: Settings) -> None:
    """Включить все разделы в собранных `Settings`, если выключатель стоит на staging."""
    if not settings.staging_all_features:
        return
    refusals = staging_contour_refusals(settings)
    if refusals:
        logger.error(
            "staging_all_features_refused",
            error_code=STAGING_ALL_FEATURES_REFUSED_ERROR_CODE,
            reasons=refusals,
        )
        return
    for name in type(settings).model_fields:
        if name.startswith("enable_") and name not in STAGING_ALL_FEATURES_EXCLUDED:
            setattr(settings, name, True)
    for name, (model, provider) in STAGING_SECTION_MODELS.items():
        if not getattr(settings, name) and _provider_key(settings, provider):
            setattr(settings, name, model)


def staging_models_without_key(settings: Settings) -> list[str]:
    """Модели разделов, оставшиеся пустыми при выключателе: нет ключа провайдера."""
    if not settings.staging_all_features or staging_contour_refusals(settings):
        return []
    return [name for name in STAGING_SECTION_MODELS if not getattr(settings, name)]


def _provider_key(settings: Settings, provider: SectionProvider) -> str:
    return settings.openai_api_key if provider == "openai" else settings.anthropic_api_key
