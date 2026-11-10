"""Staging: один выключатель окружения включает все разделы (#940).

Без строки `STAGING_ALL_FEATURES` всё по-прежнему выключено — даже на контуре, который
по всем признакам похож на staging: этими признаками обладает и гостевая копия отеля.
Со строкой — каждый `enable_*` включён поверх явного `false` из compose, пустые модели
разделов получают модель из кода при ключе провайдера, и процесс с ними стартует.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from hospitality.ai.gateway.api import validate_configured_model
from hospitality.ai.gateway.price_list import MODEL_PROVIDER
from hospitality.shared.config import Settings, get_settings
from hospitality.shared.staging_features import (
    STAGING_ALL_FEATURES_EXCLUDED,
    STAGING_SECTION_MODELS,
    staging_models_without_key,
)

ENABLE_FIELDS = sorted(name for name in Settings.model_fields if name.startswith("enable_"))
SECTION_MODEL_FIELDS = sorted(
    name for name in Settings.model_fields if name.startswith("llm_model_")
)


@pytest.fixture
def section_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """Окружение без выключателей и моделей разделов; кэш настроек — чистый до и после."""
    for name in (*ENABLE_FIELDS, *SECTION_MODEL_FIELDS, "staging_all_features"):
        monkeypatch.delenv(name.upper(), raising=False)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def _settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _staging_lookalike(env: pytest.MonkeyPatch) -> None:
    """Всё, по чему контур можно принять за staging, — так выглядит и копия отеля."""
    env.setenv("SENTRY_ENVIRONMENT", "staging")
    env.setenv("PUBLIC_BASE_URL", "https://staging.necturn.com")
    env.setenv("TELEGRAM_TENANT_SLUG", "demo-hotel")
    env.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    env.setenv("OPENAI_API_KEY", "test-openai-key")


def test_without_switch_everything_stays_off_on_staging_lookalike(
    section_env: pytest.MonkeyPatch,
) -> None:
    _staging_lookalike(section_env)

    settings = _settings()

    assert not settings.staging_all_features
    assert {name: getattr(settings, name) for name in ENABLE_FIELDS} == dict.fromkeys(
        ENABLE_FIELDS, False
    )
    assert {name: getattr(settings, name) for name in SECTION_MODEL_FIELDS} == dict.fromkeys(
        SECTION_MODEL_FIELDS, ""
    )
    assert staging_models_without_key(settings) == []


def test_switch_turns_every_section_on_over_explicit_false(
    section_env: pytest.MonkeyPatch,
) -> None:
    # Compose передаёт каждый выключатель явным `${ENABLE_…:-false}`.
    for name in ENABLE_FIELDS:
        section_env.setenv(name.upper(), "false")
    section_env.setenv("STAGING_ALL_FEATURES", "true")

    settings = _settings()

    expected = {name: name not in STAGING_ALL_FEATURES_EXCLUDED for name in ENABLE_FIELDS}
    assert {name: getattr(settings, name) for name in ENABLE_FIELDS} == expected
    assert not settings.enable_handover_manual  # демо передачи смены главнее


def test_switch_fills_empty_section_models_and_keeps_set_ones(
    section_env: pytest.MonkeyPatch,
) -> None:
    _staging_lookalike(section_env)
    section_env.setenv("STAGING_ALL_FEATURES", "true")
    section_env.setenv("LLM_MODEL_IDEAS", "claude-opus-4-8")

    settings = _settings()

    assert settings.llm_model_ideas == "claude-opus-4-8"  # заданную руками не трогает
    for name, (model, _) in STAGING_SECTION_MODELS.items():
        if name != "llm_model_ideas":
            assert getattr(settings, name) == model, name
    assert settings.llm_model == "claude-sonnet-5"  # модель гостя — не раздел
    assert staging_models_without_key(settings) == []


def test_switch_leaves_model_empty_without_provider_key_and_process_starts(
    section_env: pytest.MonkeyPatch,
) -> None:
    section_env.setenv("STAGING_ALL_FEATURES", "true")
    section_env.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    section_env.setenv("OPENAI_API_KEY", "")

    settings = _settings()

    assert settings.llm_model_mentor == ""
    assert staging_models_without_key(settings) == ["llm_model_mentor"]
    validate_configured_model()  # без ключа OpenAI раздел без модели, а не отказ старта


def test_staging_section_models_pass_startup_check(section_env: pytest.MonkeyPatch) -> None:
    _staging_lookalike(section_env)
    section_env.setenv("STAGING_ALL_FEATURES", "true")

    validate_configured_model()  # прайс-лист, поток у mentor, картинки у documents, ключи

    settings = get_settings()
    assert {name: getattr(settings, name) for name in STAGING_SECTION_MODELS} == {
        name: model for name, (model, _) in STAGING_SECTION_MODELS.items()
    }


def test_section_model_provider_matches_price_list() -> None:
    for name, (model, provider) in STAGING_SECTION_MODELS.items():
        assert MODEL_PROVIDER.get(model) == provider, name


def test_every_section_model_has_staging_default() -> None:
    # Новая LLM_MODEL_* раздела — строка в STAGING_SECTION_MODELS (#940).
    assert sorted(STAGING_SECTION_MODELS) == SECTION_MODEL_FIELDS


def test_every_section_switch_is_bool_and_exclusions_are_real() -> None:
    for name in ENABLE_FIELDS:
        assert Settings.model_fields[name].annotation is bool, name
    assert set(STAGING_ALL_FEATURES_EXCLUDED) <= set(ENABLE_FIELDS)
