from datetime import UTC, date, datetime
from typing import Never, cast
from zoneinfo import ZoneInfo

import pytest

from market_sentinel.domain.models import TradingMarket
from market_sentinel.trading_calendar.exchange import (
    MARKET_CALENDAR_CODES,
    ExchangeCalendarsTradingCalendar,
    TradingCalendarOutOfBoundsError,
    TradingCalendarProviderError,
    UnknownTradingMarketError,
)


@pytest.fixture(scope="module")
def calendar() -> ExchangeCalendarsTradingCalendar:
    return ExchangeCalendarsTradingCalendar()


@pytest.mark.parametrize(
    ("market", "expected_code"),
    [
        (TradingMarket.A_SHARE, "XSHG"),
        (TradingMarket.KOREA, "XKRX"),
        (TradingMarket.US, "XNYS"),
    ],
)
def test_each_market_maps_to_expected_calendar_code(
    market: TradingMarket,
    expected_code: str,
) -> None:
    assert MARKET_CALENDAR_CODES[market] == expected_code


@pytest.mark.parametrize(
    ("market", "moment"),
    [
        (
            TradingMarket.A_SHARE,
            datetime(2026, 7, 24, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
        ),
        (
            TradingMarket.KOREA,
            datetime(2026, 7, 24, 12, tzinfo=ZoneInfo("Asia/Seoul")),
        ),
        (
            TradingMarket.US,
            datetime(2026, 7, 24, 12, tzinfo=ZoneInfo("America/New_York")),
        ),
    ],
)
def test_each_market_recognizes_fixed_trading_day(
    calendar: ExchangeCalendarsTradingCalendar,
    market: TradingMarket,
    moment: datetime,
) -> None:
    assert calendar.is_trading_day(market, moment)


@pytest.mark.parametrize(
    ("market", "moment"),
    [
        (
            TradingMarket.A_SHARE,
            datetime(2026, 7, 25, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
        ),
        (
            TradingMarket.KOREA,
            datetime(2026, 7, 25, 12, tzinfo=ZoneInfo("Asia/Seoul")),
        ),
        (
            TradingMarket.US,
            datetime(2026, 7, 25, 12, tzinfo=ZoneInfo("America/New_York")),
        ),
    ],
)
def test_each_market_recognizes_fixed_weekend(
    calendar: ExchangeCalendarsTradingCalendar,
    market: TradingMarket,
    moment: datetime,
) -> None:
    assert not calendar.is_trading_day(market, moment)


@pytest.mark.parametrize(
    ("market", "timezone_name", "holiday"),
    [
        (TradingMarket.A_SHARE, "Asia/Shanghai", date(2026, 1, 1)),
        (TradingMarket.A_SHARE, "Asia/Shanghai", date(2026, 1, 2)),
        (TradingMarket.A_SHARE, "Asia/Shanghai", date(2026, 2, 16)),
        (TradingMarket.A_SHARE, "Asia/Shanghai", date(2026, 2, 17)),
        (TradingMarket.A_SHARE, "Asia/Shanghai", date(2026, 2, 18)),
        (TradingMarket.A_SHARE, "Asia/Shanghai", date(2026, 2, 19)),
        (TradingMarket.A_SHARE, "Asia/Shanghai", date(2026, 2, 20)),
        (TradingMarket.A_SHARE, "Asia/Shanghai", date(2026, 2, 23)),
        (TradingMarket.A_SHARE, "Asia/Shanghai", date(2026, 4, 6)),
        (TradingMarket.A_SHARE, "Asia/Shanghai", date(2026, 5, 1)),
        (TradingMarket.A_SHARE, "Asia/Shanghai", date(2026, 5, 4)),
        (TradingMarket.A_SHARE, "Asia/Shanghai", date(2026, 5, 5)),
        (TradingMarket.A_SHARE, "Asia/Shanghai", date(2026, 6, 19)),
        (TradingMarket.A_SHARE, "Asia/Shanghai", date(2026, 9, 25)),
        (TradingMarket.A_SHARE, "Asia/Shanghai", date(2026, 10, 1)),
        (TradingMarket.A_SHARE, "Asia/Shanghai", date(2026, 10, 2)),
        (TradingMarket.A_SHARE, "Asia/Shanghai", date(2026, 10, 5)),
        (TradingMarket.A_SHARE, "Asia/Shanghai", date(2026, 10, 6)),
        (TradingMarket.A_SHARE, "Asia/Shanghai", date(2026, 10, 7)),
        (TradingMarket.KOREA, "Asia/Seoul", date(2026, 1, 1)),
        (TradingMarket.KOREA, "Asia/Seoul", date(2026, 2, 16)),
        (TradingMarket.KOREA, "Asia/Seoul", date(2026, 2, 17)),
        (TradingMarket.KOREA, "Asia/Seoul", date(2026, 2, 18)),
        (TradingMarket.KOREA, "Asia/Seoul", date(2026, 3, 2)),
        (TradingMarket.KOREA, "Asia/Seoul", date(2026, 5, 1)),
        (TradingMarket.KOREA, "Asia/Seoul", date(2026, 5, 5)),
        (TradingMarket.KOREA, "Asia/Seoul", date(2026, 5, 25)),
        (TradingMarket.KOREA, "Asia/Seoul", date(2026, 6, 3)),
        (TradingMarket.KOREA, "Asia/Seoul", date(2026, 8, 17)),
        (TradingMarket.KOREA, "Asia/Seoul", date(2026, 9, 24)),
        (TradingMarket.KOREA, "Asia/Seoul", date(2026, 9, 25)),
        (TradingMarket.KOREA, "Asia/Seoul", date(2026, 10, 5)),
        (TradingMarket.KOREA, "Asia/Seoul", date(2026, 10, 9)),
        (TradingMarket.KOREA, "Asia/Seoul", date(2026, 12, 25)),
        (TradingMarket.KOREA, "Asia/Seoul", date(2026, 12, 31)),
        (TradingMarket.US, "America/New_York", date(2026, 1, 1)),
        (TradingMarket.US, "America/New_York", date(2026, 1, 19)),
        (TradingMarket.US, "America/New_York", date(2026, 2, 16)),
        (TradingMarket.US, "America/New_York", date(2026, 4, 3)),
        (TradingMarket.US, "America/New_York", date(2026, 5, 25)),
        (TradingMarket.US, "America/New_York", date(2026, 6, 19)),
        (TradingMarket.US, "America/New_York", date(2026, 7, 3)),
        (TradingMarket.US, "America/New_York", date(2026, 9, 7)),
        (TradingMarket.US, "America/New_York", date(2026, 11, 26)),
        (TradingMarket.US, "America/New_York", date(2026, 12, 25)),
    ],
)
def test_all_2026_official_holidays_are_closed(
    calendar: ExchangeCalendarsTradingCalendar,
    market: TradingMarket,
    timezone_name: str,
    holiday: date,
) -> None:
    moment = datetime(
        holiday.year,
        holiday.month,
        holiday.day,
        12,
        tzinfo=ZoneInfo(timezone_name),
    )

    assert not calendar.is_trading_day(market, moment)


@pytest.mark.parametrize(
    "holiday",
    [
        date(2026, 2, 16),
        date(2026, 2, 17),
        date(2026, 2, 18),
        date(2026, 2, 19),
        date(2026, 2, 20),
        date(2026, 2, 23),
    ],
)
def test_china_spring_festival_is_closed(
    calendar: ExchangeCalendarsTradingCalendar,
    holiday: date,
) -> None:
    moment = datetime(
        holiday.year,
        holiday.month,
        holiday.day,
        12,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )

    assert not calendar.is_trading_day(TradingMarket.A_SHARE, moment)


def test_us_observed_independence_day_is_closed(
    calendar: ExchangeCalendarsTradingCalendar,
) -> None:
    moment = datetime(2026, 7, 3, 12, tzinfo=ZoneInfo("America/New_York"))

    assert not calendar.is_trading_day(TradingMarket.US, moment)


def test_korea_official_election_closure_overrides_library(
    calendar: ExchangeCalendarsTradingCalendar,
) -> None:
    election_day = datetime(2026, 6, 3, 12, tzinfo=ZoneInfo("Asia/Seoul"))

    assert not calendar.is_trading_day(TradingMarket.KOREA, election_day)


@pytest.mark.parametrize(
    ("market", "timezone_name", "trading_day"),
    [
        (TradingMarket.A_SHARE, "Asia/Shanghai", date(2026, 2, 13)),
        (TradingMarket.A_SHARE, "Asia/Shanghai", date(2026, 2, 24)),
        (TradingMarket.KOREA, "Asia/Seoul", date(2026, 6, 2)),
        (TradingMarket.KOREA, "Asia/Seoul", date(2026, 6, 4)),
        (TradingMarket.US, "America/New_York", date(2026, 4, 2)),
        (TradingMarket.US, "America/New_York", date(2026, 4, 6)),
    ],
)
def test_holiday_boundary_dates_are_trading_days(
    calendar: ExchangeCalendarsTradingCalendar,
    market: TradingMarket,
    timezone_name: str,
    trading_day: date,
) -> None:
    moment = datetime(
        trading_day.year,
        trading_day.month,
        trading_day.day,
        12,
        tzinfo=ZoneInfo(timezone_name),
    )

    assert calendar.is_trading_day(market, moment)


def test_china_makeup_workday_weekend_remains_closed(
    calendar: ExchangeCalendarsTradingCalendar,
) -> None:
    makeup_workday = datetime(2026, 2, 28, 12, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert not calendar.is_trading_day(TradingMarket.A_SHARE, makeup_workday)


@pytest.mark.parametrize(
    "early_close",
    [
        date(2026, 11, 27),
        date(2026, 12, 24),
    ],
)
def test_nyse_early_close_dates_are_still_trading_days(
    calendar: ExchangeCalendarsTradingCalendar,
    early_close: date,
) -> None:
    moment = datetime(
        early_close.year,
        early_close.month,
        early_close.day,
        12,
        tzinfo=ZoneInfo("America/New_York"),
    )

    assert calendar.is_trading_day(TradingMarket.US, moment)


def test_same_utc_moment_uses_each_markets_local_date(
    calendar: ExchangeCalendarsTradingCalendar,
) -> None:
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


def test_unknown_market_is_rejected(
    calendar: ExchangeCalendarsTradingCalendar,
) -> None:
    unknown_market = cast(TradingMarket, "unsupported")

    with pytest.raises(UnknownTradingMarketError, match="Unknown trading market"):
        calendar.is_trading_day(
            unknown_market,
            datetime(2026, 7, 24, 12, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2025, 12, 31, 12, tzinfo=UTC),
        datetime(2027, 1, 1, 12, tzinfo=UTC),
    ],
)
def test_unverified_date_is_rejected_as_out_of_bounds(
    calendar: ExchangeCalendarsTradingCalendar,
    moment: datetime,
) -> None:
    with pytest.raises(TradingCalendarOutOfBoundsError, match="verified range"):
        calendar.is_trading_day(TradingMarket.A_SHARE, moment)


def test_naive_datetime_is_rejected(
    calendar: ExchangeCalendarsTradingCalendar,
) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        calendar.is_trading_day(
            TradingMarket.A_SHARE,
            datetime(2026, 7, 24, 12),  # noqa: DTZ001 - deliberately tests rejection
        )


def test_calendar_initialization_failure_is_explicit() -> None:
    def failing_loader(calendar_code: str) -> Never:
        raise RuntimeError(f"failed to load {calendar_code}")

    with pytest.raises(
        TradingCalendarProviderError,
        match="Unable to initialize trading calendar XSHG",
    ) as exc_info:
        ExchangeCalendarsTradingCalendar(calendar_loader=failing_loader)

    assert isinstance(exc_info.value.__cause__, RuntimeError)


def test_calendar_query_failure_is_explicit() -> None:
    class FailingCalendar:
        def is_session(self, session_date: date) -> bool:
            raise RuntimeError(f"failed to query {session_date}")

    calendar = ExchangeCalendarsTradingCalendar(
        calendar_loader=lambda _: FailingCalendar(),
    )

    with pytest.raises(
        TradingCalendarProviderError,
        match="Unable to query trading calendar XSHG",
    ) as exc_info:
        calendar.is_trading_day(
            TradingMarket.A_SHARE,
            datetime(2026, 7, 24, 12, tzinfo=UTC),
        )

    assert isinstance(exc_info.value.__cause__, RuntimeError)
