from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    llm_provider: Literal["openai", "deepseek", "mock"] = "mock"
    trading_calendar: Literal["weekday", "exchange"] = "weekday"

    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-5.6"

    deepseek_api_key: SecretStr | None = None
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_base_url: str = "https://api.deepseek.com"

    tushare_token: SecretStr | None = None
    security_master_max_age_days: int = Field(default=7, gt=0)

    opend_host: str = "127.0.0.1"
    opend_port: int = Field(default=11111, ge=1, le=65535)
    opend_connect_timeout_seconds: float = Field(default=2.0, gt=0, le=2.0)

    notify_webhook_url: str | None = None
    portfolio_config_path: Path = Path("config/portfolio.example.yaml")
    watchlist_config_path: Path = Path("config/watchlist.yaml")
    enable_scheduler: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
