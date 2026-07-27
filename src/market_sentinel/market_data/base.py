from abc import ABC, abstractmethod
from collections.abc import Sequence

from market_sentinel.domain.models import MarketPhase, MarketSnapshot
from market_sentinel.domain.quotes import QuoteBatch


class MarketDataProvider(ABC):
    """Legacy snapshot interface currently consumed by ReportService."""

    @abstractmethod
    async def get_snapshot(self, phase: MarketPhase) -> MarketSnapshot:
        raise NotImplementedError


class QuoteMarketDataProvider(ABC):
    """Provider-independent real-time quote batch interface."""

    @abstractmethod
    async def get_quotes(
        self,
        symbols: Sequence[str],
        phase: MarketPhase,
    ) -> QuoteBatch:
        raise NotImplementedError
