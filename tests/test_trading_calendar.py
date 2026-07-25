from datetime import UTC, date, datetime

import pytest

from market_sentinel.domain.models import TradingMarket
from market_sentinel.trading_calendar.weekday import WeekdayCalendar


def test_weekend_is_skipped() -> None:
    calendar = WeekdayCalendar()
    saturday = datetime(2026, 7, 25, 12, tzinfo=UTC)

    for market in TradingMarket:
        assert not calendar.is_trading_day(market, saturday)


def test_configured_holiday_is_skipped() -> None:
    calendar = WeekdayCalendar(
        holidays={TradingMarket.A_SHARE: {date(2026, 10, 1)}},
    )
    holiday_in_shanghai = datetime(2026, 9, 30, 16, 30, tzinfo=UTC)

    assert not calendar.is_trading_day(
        TradingMarket.A_SHARE,
        holiday_in_shanghai,
    )


def test_same_moment_uses_each_markets_local_timezone() -> None:
    calendar = WeekdayCalendar()
    friday_utc_after_asia_midnight = datetime(2026, 7, 24, 16, 30, tzinfo=UTC)

    assert not calendar.is_trading_day(
        TradingMarket.A_SHARE,
        friday_utc_after_asia_midnight,
    )
    assert not calendar.is_trading_day(
        TradingMarket.KOREA,
        friday_utc_after_asia_midnight,
    )
    assert calendar.is_trading_day(
        TradingMarket.US,
        friday_utc_after_asia_midnight,
    )


def test_naive_datetime_is_rejected() -> None:
    calendar = WeekdayCalendar()

    with pytest.raises(ValueError, match="timezone-aware"):
        calendar.is_trading_day(
            TradingMarket.A_SHARE,
            datetime(2026, 7, 24, 9),  # noqa: DTZ001 - deliberately tests rejection
        )
