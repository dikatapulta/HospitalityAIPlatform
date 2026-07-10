"""Типизированные настройки приложения (Task 0005, FOUNDATION P-7).

Единственный канонический способ прочитать конфигурацию окружения. Значения
читаются из переменных окружения / `.env` (см. `.env.example`); значения по
умолчанию совпадают с `.env.example`, чтобы `pytest`/`make check` работали без
дополнительной настройки, а `docker compose` (Task 0004) переопределял их
реальными значениями сети контейнеров.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "hospitality"
    postgres_password: str = "hospitality"
    postgres_db: str = "hospitality"

    redis_host: str = "localhost"
    redis_port: int = 6379

    app_port: int = 8000

    # Воркер доменных событий (Task 0010, ADR-005): период опроса outbox при
    # пустой очереди, размер пачки и предел попыток доставки одного события
    # (исчерпание — ERR-EVENTS-002 в docs/runbooks/errors.md).
    worker_poll_interval_seconds: float = 1.0
    worker_batch_size: int = 50
    worker_max_delivery_attempts: int = 10

    # Literal, а не str: опечатка в LOG_LEVEL должна падать здесь внятной ошибкой
    # конфигурации, а не ValueError из глубин logging при старте (crash-loop
    # контейнера с непонятным трейсбеком).
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> object:
        # LOG_LEVEL=info или значение с пробелом — валидная конфигурация.
        return value.strip().upper() if isinstance(value, str) else value

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def postgres_dsn_async(self) -> str:
        # DSN для SQLAlchemy async engine (Task 0008): тот же Postgres,
        # но с явным драйвером asyncpg в схеме URL.
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_dsn(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/0"


@lru_cache
def get_settings() -> Settings:
    return Settings()
