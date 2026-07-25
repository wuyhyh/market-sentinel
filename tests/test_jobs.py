from datetime import UTC, datetime

import pytest

from market_sentinel.domain.models import (
    ActionState,
    Fact,
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
from market_sentinel.llm.base import LLMAnalyst
from market_sentinel.market_data.base import MarketDataProvider
from market_sentinel.notifications.base import Notifier
from market_sentinel.trading_calendar.base import PHASE_MARKETS, TradingCalendar

NOW = datetime(2026, 7, 24, 2, tzinfo=UTC)


class RecordingCalendar(TradingCalendar):
    def __init__(self, *, is_open: bool) -> None:
        self.is_open = is_open
        self.calls: list[tuple[TradingMarket, datetime]] = []

    def is_trading_day(self, market: TradingMarket, moment: datetime) -> bool:
        self.calls.append((market, moment))
        return self.is_open


class RecordingDataProvider(MarketDataProvider):
    def __init__(self) -> None:
        self.calls: list[MarketPhase] = []

    async def get_snapshot(self, phase: MarketPhase) -> MarketSnapshot:
        self.calls.append(phase)
        return MarketSnapshot(
            phase=phase,
            generated_at=NOW,
            facts=[
                Fact(
                    statement="开发测试事实",
                    source="test://market-data",
                    source_time=NOW,
                )
            ],
        )


class RecordingAnalyst(LLMAnalyst):
    def __init__(self) -> None:
        self.calls = 0

    async def analyze(
        self,
        snapshot: MarketSnapshot,
        risk_report: RiskReport,
    ) -> MarketBrief:
        self.calls += 1
        return MarketBrief(
            title="测试简报",
            generated_at=NOW,
            phase=snapshot.phase,
            facts=snapshot.facts,
            portfolio_risks=[item.message for item in risk_report.violations],
            action_state=ActionState.NO_ACTION,
            confidence=0.1,
            disclaimer="仅供测试，任何交易由用户人工判断。",
        )


class RecordingNotifier(Notifier):
    def __init__(self) -> None:
        self.calls = 0

    async def send(self, brief: MarketBrief) -> None:
        self.calls += 1


def build_service(
    calendar: TradingCalendar,
) -> tuple[ReportService, RecordingDataProvider, RecordingAnalyst, RecordingNotifier]:
    data_provider = RecordingDataProvider()
    analyst = RecordingAnalyst()
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
        clock=lambda: NOW,
    )
    return service, data_provider, analyst, notifier


@pytest.mark.parametrize("phase", list(MarketPhase))
async def test_every_job_checks_calendar_before_reading_data(
    phase: MarketPhase,
) -> None:
    calendar = RecordingCalendar(is_open=False)
    service, data_provider, analyst, notifier = build_service(calendar)

    status = await service.run(phase)

    assert status is JobRunStatus.SKIPPED_NON_TRADING_DAY
    assert calendar.calls == [(PHASE_MARKETS[phase], NOW)]
    assert data_provider.calls == []
    assert analyst.calls == 0
    assert notifier.calls == 0


async def test_trading_day_runs_report_pipeline() -> None:
    calendar = RecordingCalendar(is_open=True)
    service, data_provider, analyst, notifier = build_service(calendar)

    status = await service.run(MarketPhase.A_SHARE_CLOSE)

    assert status is JobRunStatus.COMPLETED
    assert data_provider.calls == [MarketPhase.A_SHARE_CLOSE]
    assert analyst.calls == 1
    assert notifier.calls == 1


def test_every_market_phase_has_a_calendar_mapping() -> None:
    assert set(PHASE_MARKETS) == set(MarketPhase)
