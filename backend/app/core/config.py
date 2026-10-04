"""Application configuration.

Secrets are read from the environment only. Nothing here may be logged,
persisted into a snapshot, or returned through the API.
"""

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnv(StrEnum):
    development = "development"
    production = "production"
    test = "test"


class Role(StrEnum):
    api = "api"
    worker = "worker"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: AppEnv = AppEnv.development
    log_level: str = "INFO"
    careerlens_role: Role = Role.api

    database_url: SecretStr
    redis_url: SecretStr

    uvicorn_workers: int = 2

    # "none" until the provider abstraction lands in Milestone 8. The
    # application must never depend directly on one vendor.
    ai_provider: str = "none"
    ai_api_key: SecretStr | None = None
    ai_model: str | None = None

    test_database_url: SecretStr | None = Field(default=None)

    @property
    def ai_enabled(self) -> bool:
        return self.ai_provider != "none"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
