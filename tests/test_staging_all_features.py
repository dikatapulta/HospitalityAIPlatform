"""Staging: один выключатель окружения включает все разделы (#940).

Без строки `STAGING_ALL_FEATURES` всё по-прежнему выключено — даже на контуре, который
по всем признакам staging. Со строкой на staging — каждый `enable_*` включён поверх явного
`false` из compose, пустые модели разделов получают модель из кода при ключе провайдера,
и процесс с ними стартует. Строка на контуре, где не сошёлся хоть один признак staging, —
копия отеля, куда её перенесли из `.env` staging, — разделы не трогает и пишет ошибку
ERR-PLATFORM-012 (#1053).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from structlog.testing import capture_logs

from hospitality.ai.gateway.api import validate_configured_model
from hospitality.ai.gateway.price_list import MODEL_PROVIDER
from hospitality.platform.seed import DEMO_TENANT_SLUG
from hospitality.shared.config import Settings, get_settings
from hospitality.shared.staging_features import (
    STAGING_ALL_FEATURES_EXCLUDED,
    STAGING_ALL_FEATURES_REFUSED_ERROR_CODE,
    STAGING_SECTION_MODELS,
    STAGING_TENANT_SLUG,
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
    """Все три признака staging и ключи провайдеров."""
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
    _staging_lookalike(section_env)
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
    _staging_lookalike(section_env)
    section_env.setenv("STAGING_ALL_FEATURES", "true")
    section_env.setenv("OPENAI_API_KEY", "")

    settings = _settings()

    assert settings.llm_model_mentor == ""
    assert staging_models_without_key(settings) == ["llm_model_mentor"]
    validate_configured_model()  # без ключа OpenAI раздел без модели, а не отказ старта


@pytest.mark.parametrize(
    "copy_signs",
    [
        # Копия отеля целиком: своя метка, свой отель, свой адрес.
        {
            "SENTRY_ENVIRONMENT": "pilot",
            "TELEGRAM_TENANT_SLUG": "pilot-hotel",
            "PUBLIC_BASE_URL": "https://app.example.test",
        },
        # Копия, где строку SENTRY_ENVIRONMENT=pilot забыли (#389): отсекают отель и адрес.
        {"TELEGRAM_TENANT_SLUG": "pilot-hotel", "PUBLIC_BASE_URL": "https://app.example.test"},
        # Каждый признак по отдельности — выключателю нужны все три.
        {"SENTRY_ENVIRONMENT": "pilot"},
        {"TELEGRAM_TENANT_SLUG": "pilot-hotel"},
        {"PUBLIC_BASE_URL": "https://app.example.test"},
    ],
)
def test_switch_off_staging_leaves_sections_off_and_logs_error(
    section_env: pytest.MonkeyPatch, copy_signs: dict[str, str]
) -> None:
    for name in ENABLE_FIELDS:
        section_env.setenv(name.upper(), "false")
    _staging_lookalike(section_env)
    for name, value in copy_signs.items():
        section_env.setenv(name, value)
    section_env.setenv("STAGING_ALL_FEATURES", "true")

    with capture_logs() as logs:
        settings = _settings()

    assert {name: getattr(settings, name) for name in ENABLE_FIELDS} == dict.fromkeys(
        ENABLE_FIELDS, False
    )
    assert {name: getattr(settings, name) for name in SECTION_MODEL_FIELDS} == dict.fromkeys(
        SECTION_MODEL_FIELDS, ""
    )
    assert staging_models_without_key(settings) == []
    refused = [entry for entry in logs if entry["event"] == "staging_all_features_refused"]
    assert len(refused) == 1, logs
    assert refused[0]["log_level"] == "error"
    assert refused[0]["error_code"] == STAGING_ALL_FEATURES_REFUSED_ERROR_CODE
    assert len(refused[0]["reasons"]) == len(copy_signs)
    assert "pilot-hotel" not in str(refused[0])  # код отеля в логи не попадает


def test_staging_tenant_slug_is_demo_hotel() -> None:
    # Ядро не импортирует `platform/seed.py` (R-5) — совпадение держит тест.
    assert STAGING_TENANT_SLUG == DEMO_TENANT_SLUG


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
