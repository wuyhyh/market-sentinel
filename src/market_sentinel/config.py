from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "development"
    log_level: str = "INFO"

    llm_provider: Literal["openai", "deepseek", "mock"] = "mock"

    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-5.6"

    deepseek_api_key: SecretStr | None = None
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_base_url: str = "https://api.deepseek.com"

    notify_webhook_url: str | None = None
    enable_scheduler: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
