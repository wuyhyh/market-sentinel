from abc import ABC, abstractmethod

from market_sentinel.domain.models import MarketPhase, MarketSnapshot


class MarketDataProvider(ABC):
    @abstractmethod
    async def get_snapshot(self, phase: MarketPhase) -> MarketSnapshot:
        raise NotImplementedError
