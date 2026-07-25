from market_sentinel.domain.models import MarketPhase, Position, RiskPolicy
from market_sentinel.llm.base import LLMAnalyst
from market_sentinel.market_data.base import MarketDataProvider
from market_sentinel.notifications.base import Notifier
from market_sentinel.risk_engine import evaluate_portfolio


class ReportService:
    def __init__(
        self,
        *,
        data_provider: MarketDataProvider,
        analyst: LLMAnalyst,
        notifier: Notifier,
        risk_policy: RiskPolicy,
        positions: list[Position],
    ) -> None:
        self._data_provider = data_provider
        self._analyst = analyst
        self._notifier = notifier
        self._risk_policy = risk_policy
        self._positions = positions

    async def run(self, phase: MarketPhase) -> None:
        snapshot = await self._data_provider.get_snapshot(phase)
        risk_report = evaluate_portfolio(self._risk_policy, self._positions)
        brief = await self._analyst.analyze(snapshot, risk_report)
        await self._notifier.send(brief)
