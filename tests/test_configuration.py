import pytest
from pydantic import SecretStr, ValidationError

from market_sentinel.config import Settings
from market_sentinel.llm.deepseek_provider import DeepSeekAnalyst
from market_sentinel.llm.factory import build_analyst
from market_sentinel.llm.mock_provider import MockAnalyst
from market_sentinel.llm.openai_provider import OpenAIAnalyst
from market_sentinel.trading_calendar.factory import build_trading_calendar
from market_sentinel.trading_calendar.weekday import WeekdayCalendar


@pytest.mark.parametrize(
    ("settings", "expected_type"),
    [
        (Settings(llm_provider="mock"), MockAnalyst),
        (
            Settings(llm_provider="openai", openai_api_key=SecretStr("test-key")),
            OpenAIAnalyst,
        ),
        (
            Settings(llm_provider="deepseek", deepseek_api_key=SecretStr("test-key")),
            DeepSeekAnalyst,
        ),
    ],
)
def test_llm_backends_remain_selectable(
    settings: Settings,
    expected_type: type[MockAnalyst | OpenAIAnalyst | DeepSeekAnalyst],
) -> None:
    assert isinstance(build_analyst(settings), expected_type)


def test_weekday_calendar_is_available_in_development() -> None:
    calendar = build_trading_calendar(
        Settings(app_env="development", trading_calendar="weekday"),
    )

    assert isinstance(calendar, WeekdayCalendar)


def test_weekday_calendar_is_rejected_in_production() -> None:
    with pytest.raises(RuntimeError, match="development-only"):
        build_trading_calendar(
            Settings(app_env="production", trading_calendar="weekday"),
        )


def test_unknown_llm_provider_is_rejected_by_configuration() -> None:
    with pytest.raises(ValidationError):
        Settings(llm_provider="unsupported")  # type: ignore[arg-type]
