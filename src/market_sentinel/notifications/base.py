from abc import ABC, abstractmethod

from market_sentinel.domain.models import MarketBrief


class Notifier(ABC):
    @abstractmethod
    async def send(self, brief: MarketBrief) -> None:
        raise NotImplementedError
