from datetime import UTC, datetime

from market_sentinel.domain.models import Fact, MarketPhase, MarketSnapshot
from market_sentinel.market_data.base import MarketDataProvider


class MockMarketDataProvider(MarketDataProvider):
    async def get_snapshot(self, phase: MarketPhase) -> MarketSnapshot:
        now = datetime.now(UTC)
        return MarketSnapshot(
            phase=phase,
            generated_at=now,
            facts=[
                Fact(
                    statement="当前是开发环境 Mock 数据，未连接真实行情。",
                    source="mock://market-data",
                    source_time=now,
                    freshness_note="禁止用于真实交易",
                )
            ],
            raw_metrics={},
        )
