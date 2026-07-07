"""Типизированные настройки приложения (Task 0005, FOUNDATION P-7).

Единственный канонический способ прочитать конфигурацию окружения. Значения
читаются из переменных окружения / `.env` (см. `.env.example`); значения по
умолчанию совпадают с `.env.example`, чтобы `pytest`/`make check` работали без
дополнительной настройки, а `docker compose` (Task 0004) переопределял их
реальными значениями сети контейнеров.
"""

from __future__ import annotations

from functools import lru_cache

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

    log_level: str = "INFO"

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_dsn(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/0"


@lru_cache
def get_settings() -> Settings:
    return Settings()
