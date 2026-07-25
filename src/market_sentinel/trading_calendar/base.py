from abc import ABC, abstractmethod
from datetime import datetime
from zoneinfo import ZoneInfo

from market_sentinel.domain.models import MarketPhase, TradingMarket

MARKET_TIMEZONES: dict[TradingMarket, ZoneInfo] = {
    TradingMarket.A_SHARE: ZoneInfo("Asia/Shanghai"),
    TradingMarket.KOREA: ZoneInfo("Asia/Seoul"),
    TradingMarket.US: ZoneInfo("America/New_York"),
}

PHASE_MARKETS: dict[MarketPhase, TradingMarket] = {
    MarketPhase.OVERNIGHT: TradingMarket.A_SHARE,
    MarketPhase.A_SHARE_CALL_AUCTION: TradingMarket.A_SHARE,
    MarketPhase.A_SHARE_OPEN_PRICE: TradingMarket.A_SHARE,
    MarketPhase.A_SHARE_MIDDAY: TradingMarket.A_SHARE,
    MarketPhase.A_SHARE_CLOSE: TradingMarket.A_SHARE,
    MarketPhase.KOREA_OPEN: TradingMarket.KOREA,
    MarketPhase.KOREA_CLOSE: TradingMarket.KOREA,
    MarketPhase.US_OPEN: TradingMarket.US,
    MarketPhase.US_CLOSE: TradingMarket.US,
}


class TradingCalendar(ABC):
    """Exchange-calendar boundary used before any report job reads market data."""

    @abstractmethod
    def is_trading_day(self, market: TradingMarket, moment: datetime) -> bool:
        """Return whether ``moment`` falls on a trading day in ``market``."""

        raise NotImplementedError
