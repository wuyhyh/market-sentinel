from datetime import UTC, datetime

from market_sentinel.domain.models import (
    JobRunStatus,
    MarketBrief,
    MarketPhase,
    MarketSnapshot,
    Position,
    RiskPolicy,
    RiskReport,
    TradingMarket,
)
from market_sentinel.jobs import ReportService
from market_sentinel.llm.mock_provider import MockAnalyst
from market_sentinel.market_data.mock import MockMarketDataProvider
from market_sentinel.notifications.base import Notifier
from market_sentinel.trading_calendar.base import PHASE_MARKETS, TradingCalendar

FIXED_NOW = datetime(2026, 7, 24, 2, tzinfo=UTC)
TEST_PHASE = MarketPhase.A_SHARE_CLOSE


class RecordingCalendar(TradingCalendar):
    def __init__(self, *, is_open: bool) -> None:
        self._is_open = is_open
        self.calls: list[tuple[TradingMarket, datetime]] = []

    def is_trading_day(self, market: TradingMarket, moment: datetime) -> bool:
        self.calls.append((market, moment))
        return self._is_open


class RecordingMockMarketDataProvider(MockMarketDataProvider):
    def __init__(self) -> None:
        self.calls: list[MarketPhase] = []

    async def get_snapshot(self, phase: MarketPhase) -> MarketSnapshot:
        self.calls.append(phase)
        return await super().get_snapshot(phase)


class RecordingMockAnalyst(MockAnalyst):
    def __init__(self) -> None:
        self.calls: list[tuple[MarketSnapshot, RiskReport]] = []
        self.briefs: list[MarketBrief] = []

    async def analyze(
        self,
        snapshot: MarketSnapshot,
        risk_report: RiskReport,
    ) -> MarketBrief:
        self.calls.append((snapshot, risk_report))
        brief = await super().analyze(snapshot, risk_report)
        self.briefs.append(brief)
        return brief


class RecordingNotifier(Notifier):
    def __init__(self) -> None:
        self.briefs: list[MarketBrief] = []

    async def send(self, brief: MarketBrief) -> None:
        self.briefs.append(brief)


def build_service(
    calendar: TradingCalendar,
) -> tuple[
    ReportService,
    RecordingMockMarketDataProvider,
    RecordingMockAnalyst,
    RecordingNotifier,
]:
    data_provider = RecordingMockMarketDataProvider()
    analyst = RecordingMockAnalyst()
    notifier = RecordingNotifier()
    service = ReportService(
        data_provider=data_provider,
        analyst=analyst,
        notifier=notifier,
        trading_calendar=calendar,
        risk_policy=RiskPolicy(
            total_capital=500_000,
            minimum_cash=150_000,
            max_total_stock_value=100_000,
            max_single_stock_value=35_000,
        ),
        positions=[
            Position(
                symbol="CASH.CNY",
                name="现金",
                asset_type="cash",
                market_value=500_000,
                cost_value=500_000,
            )
        ],
        clock=lambda: FIXED_NOW,
    )
    return service, data_provider, analyst, notifier


async def test_mock_pipeline_completes_on_trading_day() -> None:
    calendar = RecordingCalendar(is_open=True)
    service, data_provider, analyst, notifier = build_service(calendar)

    status = await service.run(TEST_PHASE)

    assert status is JobRunStatus.COMPLETED
    assert calendar.calls == [(PHASE_MARKETS[TEST_PHASE], FIXED_NOW)]
    assert data_provider.calls == [TEST_PHASE]
    assert len(analyst.calls) == 1
    assert analyst.calls[0][0].facts[0].source == "mock://market-data"
    assert analyst.calls[0][1].passed
    assert len(analyst.briefs) == 1
    assert len(notifier.briefs) == 1
    assert notifier.briefs == analyst.briefs
    assert notifier.briefs[0].phase is TEST_PHASE


async def test_mock_pipeline_skips_on_non_trading_day() -> None:
    calendar = RecordingCalendar(is_open=False)
    service, data_provider, analyst, notifier = build_service(calendar)

    status = await service.run(TEST_PHASE)

    assert status is JobRunStatus.SKIPPED_NON_TRADING_DAY
    assert calendar.calls == [(PHASE_MARKETS[TEST_PHASE], FIXED_NOW)]
    assert data_provider.calls == []
    assert analyst.calls == []
    assert analyst.briefs == []
    assert notifier.briefs == []
