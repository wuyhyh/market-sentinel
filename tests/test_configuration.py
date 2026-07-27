from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import SecretStr, ValidationError

from market_sentinel import bootstrap
from market_sentinel.config import Settings
from market_sentinel.llm.deepseek_provider import DeepSeekAnalyst
from market_sentinel.llm.factory import build_analyst
from market_sentinel.llm.mock_provider import MockAnalyst
from market_sentinel.llm.openai_provider import OpenAIAnalyst
from market_sentinel.trading_calendar.exchange import ExchangeCalendarsTradingCalendar
from market_sentinel.trading_calendar.factory import build_trading_calendar
from market_sentinel.trading_calendar.weekday import WeekdayCalendar


def make_settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def isolate_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for field_name in Settings.model_fields:
        monkeypatch.delenv(field_name, raising=False)
        monkeypatch.delenv(field_name.upper(), raising=False)


@pytest.mark.parametrize(
    ("settings_values", "expected_type"),
    [
        ({"llm_provider": "mock"}, MockAnalyst),
        (
            {
                "llm_provider": "openai",
                "openai_api_key": SecretStr("test-key"),
            },
            OpenAIAnalyst,
        ),
        (
            {
                "llm_provider": "deepseek",
                "deepseek_api_key": SecretStr("test-key"),
            },
            DeepSeekAnalyst,
        ),
    ],
)
def test_llm_backends_remain_selectable(
    settings_values: dict[str, object],
    expected_type: type[MockAnalyst | OpenAIAnalyst | DeepSeekAnalyst],
) -> None:
    settings = make_settings(**settings_values)

    assert isinstance(build_analyst(settings), expected_type)


@pytest.mark.parametrize("app_env", ["development", "test"])
def test_weekday_calendar_is_available_in_non_production(
    app_env: Literal["development", "test"],
) -> None:
    calendar = build_trading_calendar(
        make_settings(
            app_env=app_env,
            trading_calendar="weekday",
        ),
    )

    assert isinstance(calendar, WeekdayCalendar)


def test_weekday_calendar_is_rejected_in_production() -> None:
    with pytest.raises(RuntimeError, match="development-only"):
        build_trading_calendar(
            make_settings(
                app_env="production",
                trading_calendar="weekday",
            ),
        )


def test_exchange_calendar_is_used_in_production() -> None:
    calendar = build_trading_calendar(
        make_settings(
            app_env="production",
            trading_calendar="exchange",
        ),
    )

    assert isinstance(calendar, ExchangeCalendarsTradingCalendar)


def test_unknown_trading_calendar_is_rejected_by_configuration() -> None:
    with pytest.raises(ValidationError):
        make_settings(trading_calendar="unsupported")


def test_default_settings_use_mock_analyst() -> None:
    settings = make_settings()

    assert settings.llm_provider == "mock"
    assert isinstance(build_analyst(settings), MockAnalyst)


def test_default_settings_use_offline_mock_market_data() -> None:
    assert make_settings().market_data_provider == "mock"


def test_unknown_market_data_provider_is_rejected_by_configuration() -> None:
    with pytest.raises(ValidationError):
        make_settings(market_data_provider="unknown")


def test_watchlist_path_can_be_set_by_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watchlist_path = tmp_path / "watchlist.yaml"
    monkeypatch.setenv("WATCHLIST_CONFIG_PATH", str(watchlist_path))

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.watchlist_config_path == watchlist_path


@pytest.mark.parametrize(
    ("provider", "expected_error"),
    [
        ("openai", "OPENAI_API_KEY is required"),
        ("deepseek", "DEEPSEEK_API_KEY is required"),
    ],
)
def test_paid_llm_provider_requires_api_key(
    provider: Literal["openai", "deepseek"],
    expected_error: str,
) -> None:
    settings = make_settings(llm_provider=provider)

    with pytest.raises(RuntimeError, match=expected_error):
        build_analyst(settings)


def test_unknown_llm_provider_is_rejected_by_configuration() -> None:
    with pytest.raises(ValidationError):
        make_settings(llm_provider="unsupported")


def test_api_keys_are_redacted_from_settings_output() -> None:
    secret_value = "stage-3-test-secret"
    settings = make_settings(
        openai_api_key=SecretStr(secret_value),
        deepseek_api_key=SecretStr(secret_value),
    )

    rendered_settings = (
        repr(settings),
        str(settings),
        settings.model_dump_json(),
    )

    assert all(secret_value not in rendered for rendered in rendered_settings)
    assert all("**********" in rendered for rendered in rendered_settings)


def test_tushare_token_is_a_redacted_secret() -> None:
    secret_value = "tushare-secret-that-must-not-appear"
    settings = make_settings(tushare_token=SecretStr(secret_value))

    rendered_settings = (
        repr(settings),
        str(settings),
        settings.model_dump_json(),
    )

    assert isinstance(settings.tushare_token, SecretStr)
    assert all(secret_value not in rendered for rendered in rendered_settings)
    assert all("**********" in rendered for rendered in rendered_settings)


@pytest.mark.parametrize("path_kind", ["missing", "directory"])
def test_invalid_portfolio_config_path_fails_explicitly(
    path_kind: Literal["missing", "directory"],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid_path = tmp_path / "missing.yaml" if path_kind == "missing" else tmp_path
    settings = make_settings(
        llm_provider="mock",
        portfolio_config_path=invalid_path,
    )
    monkeypatch.setattr(bootstrap, "get_settings", lambda: settings)

    with pytest.raises(
        RuntimeError,
        match="Unable to read portfolio configuration",
    ) as exc_info:
        bootstrap.build_report_service()

    assert isinstance(exc_info.value.__cause__, OSError)
