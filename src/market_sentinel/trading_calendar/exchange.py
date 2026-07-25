from collections.abc import Callable
from datetime import date, datetime
from typing import Protocol, cast

import exchange_calendars
from exchange_calendars.errors import DateOutOfBounds

from market_sentinel.domain.models import TradingMarket
from market_sentinel.trading_calendar.base import MARKET_TIMEZONES, TradingCalendar

MARKET_CALENDAR_CODES: dict[TradingMarket, str] = {
    TradingMarket.A_SHARE: "XSHG",
    TradingMarket.KOREA: "XKRX",
    TradingMarket.US: "XNYS",
}

VERIFIED_DATE_RANGES: dict[TradingMarket, tuple[date, date]] = {
    market: (date(2026, 1, 1), date(2026, 12, 31)) for market in TradingMarket
}

# KRX rules close the market on national election days. Both calendar candidates
# tested for ADR-0001 omit this 2026 local-election closure.
OFFICIAL_CLOSURES: dict[TradingMarket, dict[date, tuple[str, ...]]] = {
    TradingMarket.KOREA: {
        date(2026, 6, 3): (
            (
                "https://global.krx.co.kr/contents/GLB/06/0602/0602010201/"
                "GLB0602010201T1.jsp"
            ),
            (
                "https://kind.krx.co.kr/external/dst/reference/11625/"
                "2026%20%EC%BD%94%EC%8A%A4%EB%8B%A5%EC%8B%9C%EC%9E%A5%20"
                "%EA%B3%B5%EC%8B%9C%EC%9D%BC%EC%A0%95%20%EC%BA%98%EB%A6%B0"
                "%EB%8D%94_vF.pdf"
            ),
        )
    }
}


class TradingCalendarError(RuntimeError):
    """Base error for failures at the real trading-calendar boundary."""


class UnknownTradingMarketError(TradingCalendarError):
    """Raised when no exchange calendar is configured for a market."""


class TradingCalendarOutOfBoundsError(TradingCalendarError):
    """Raised when a date is outside the calendar's verified or supported range."""


class TradingCalendarProviderError(TradingCalendarError):
    """Raised when the exchange-calendars provider cannot answer a query."""


class _SessionCalendar(Protocol):
    def is_session(self, session_date: date) -> bool:
        """Return whether a local date is an exchange session."""


CalendarLoader = Callable[[str], _SessionCalendar]


def _load_calendar(calendar_code: str) -> _SessionCalendar:
    return cast(_SessionCalendar, exchange_calendars.get_calendar(calendar_code))


class ExchangeCalendarsTradingCalendar(TradingCalendar):
    """Production calendar backed by locally installed exchange-calendars data."""

    def __init__(self, calendar_loader: CalendarLoader | None = None) -> None:
        loader = calendar_loader or _load_calendar
        self._calendars: dict[TradingMarket, _SessionCalendar] = {}

        for market, calendar_code in MARKET_CALENDAR_CODES.items():
            try:
                self._calendars[market] = loader(calendar_code)
            except Exception as exc:
                raise TradingCalendarProviderError(
                    f"Unable to initialize trading calendar {calendar_code} "
                    f"for market {market.value}"
                ) from exc

    def is_trading_day(self, market: TradingMarket, moment: datetime) -> bool:
        try:
            calendar_code = MARKET_CALENDAR_CODES[market]
            market_timezone = MARKET_TIMEZONES[market]
            calendar = self._calendars[market]
            verified_start, verified_end = VERIFIED_DATE_RANGES[market]
        except (KeyError, TypeError) as exc:
            raise UnknownTradingMarketError(f"Unknown trading market: {market!r}") from exc

        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("Trading calendar requires a timezone-aware datetime")

        local_date = moment.astimezone(market_timezone).date()
        if not verified_start <= local_date <= verified_end:
            raise TradingCalendarOutOfBoundsError(
                f"Trading calendar date {local_date.isoformat()} for market "
                f"{market.value} ({calendar_code}) is outside the verified range "
                f"{verified_start.isoformat()} through {verified_end.isoformat()}"
            )

        try:
            is_session = calendar.is_session(local_date)
        except DateOutOfBounds as exc:
            raise TradingCalendarOutOfBoundsError(
                f"Trading calendar date {local_date.isoformat()} is outside the "
                f"supported range for market {market.value} ({calendar_code})"
            ) from exc
        except Exception as exc:
            raise TradingCalendarProviderError(
                f"Unable to query trading calendar {calendar_code} for "
                f"{local_date.isoformat()}"
            ) from exc

        if local_date in OFFICIAL_CLOSURES.get(market, {}):
            return False
        return is_session
