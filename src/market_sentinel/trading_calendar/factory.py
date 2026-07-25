from market_sentinel.config import Settings
from market_sentinel.trading_calendar.base import TradingCalendar
from market_sentinel.trading_calendar.exchange import ExchangeCalendarsTradingCalendar
from market_sentinel.trading_calendar.weekday import WeekdayCalendar


def build_trading_calendar(settings: Settings) -> TradingCalendar:
    if settings.trading_calendar == "weekday":
        if settings.app_env not in {"development", "test"}:
            raise RuntimeError(
                "WeekdayCalendar is development-only; production requires a real "
                "exchange calendar adapter"
            )
        return WeekdayCalendar()

    if settings.trading_calendar == "exchange":
        return ExchangeCalendarsTradingCalendar()

    raise RuntimeError(f"Unsupported trading calendar: {settings.trading_calendar}")
