from collections.abc import Iterable, Mapping
from datetime import date, datetime

from market_sentinel.domain.models import TradingMarket
from market_sentinel.trading_calendar.base import MARKET_TIMEZONES, TradingCalendar


class WeekdayCalendar(TradingCalendar):
    """Development-only calendar that treats configured weekdays as trading days."""

    def __init__(
        self,
        holidays: Mapping[TradingMarket, Iterable[date]] | None = None,
    ) -> None:
        holidays = holidays or {}
        self._holidays = {market: frozenset(holidays.get(market, ())) for market in TradingMarket}

    def is_trading_day(self, market: TradingMarket, moment: datetime) -> bool:
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("Trading calendar requires a timezone-aware datetime")

        local_date = moment.astimezone(MARKET_TIMEZONES[market]).date()
        return local_date.weekday() < 5 and local_date not in self._holidays[market]
