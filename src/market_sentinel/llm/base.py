from abc import ABC, abstractmethod

from market_sentinel.domain.models import MarketBrief, MarketSnapshot, RiskReport


class LLMAnalyst(ABC):
    @abstractmethod
    async def analyze(
        self,
        snapshot: MarketSnapshot,
        risk_report: RiskReport,
    ) -> MarketBrief:
        raise NotImplementedError
