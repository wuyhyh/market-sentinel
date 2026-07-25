import logging
from collections.abc import Callable
from datetime import UTC, datetime

from market_sentinel.domain.models import JobRunStatus, MarketPhase, Position, RiskPolicy
from market_sentinel.llm.base import LLMAnalyst
from market_sentinel.market_data.base import MarketDataProvider
from market_sentinel.notifications.base import Notifier
from market_sentinel.risk_engine import evaluate_portfolio
from market_sentinel.trading_calendar.base import PHASE_MARKETS, TradingCalendar

logger = logging.getLogger(__name__)


class ReportService:
    def __init__(
        self,
        *,
        data_provider: MarketDataProvider,
        analyst: LLMAnalyst,
        notifier: Notifier,
        trading_calendar: TradingCalendar,
        risk_policy: RiskPolicy,
        positions: list[Position],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._data_provider = data_provider
        self._analyst = analyst
        self._notifier = notifier
        self._trading_calendar = trading_calendar
        self._risk_policy = risk_policy
        self._positions = positions
        self._clock = clock or (lambda: datetime.now(UTC))

    async def run(self, phase: MarketPhase) -> JobRunStatus:
        moment = self._clock()
        market = PHASE_MARKETS[phase]
        if not self._trading_calendar.is_trading_day(market, moment):
            logger.info(
                "Skipping %s report because %s is not a trading day at %s",
                phase.value,
                market.value,
                moment.isoformat(),
            )
            return JobRunStatus.SKIPPED_NON_TRADING_DAY

        snapshot = await self._data_provider.get_snapshot(phase)
        risk_report = evaluate_portfolio(self._risk_policy, self._positions)
        brief = await self._analyst.analyze(snapshot, risk_report)
        await self._notifier.send(brief)
        return JobRunStatus.COMPLETED
