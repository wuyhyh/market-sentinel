from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal

from market_sentinel.domain.models import Fact, MarketPhase, MarketSnapshot, TradingMarket
from market_sentinel.domain.quotes import (
    MarketQuote,
    QuoteBatch,
    QuoteFreshness,
    QuoteMarketState,
    TradingStatus,
)
from market_sentinel.domain.security_data import (
    Currency,
    DataCompleteness,
    SecurityCategory,
    SecurityExchange,
)
from market_sentinel.market_data.base import MarketDataProvider, QuoteMarketDataProvider
from market_sentinel.market_data.errors import MarketDataQualityError


class MockMarketDataProvider(MarketDataProvider, QuoteMarketDataProvider):
    def __init__(
        self,
        security_types: Mapping[str, SecurityCategory] | None = None,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._security_types = dict(security_types or {})
        self._now = now

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

    async def get_quotes(
        self,
        symbols: Sequence[str],
        phase: MarketPhase,
    ) -> QuoteBatch:
        requested = tuple(sorted(symbols))
        if not requested:
            raise MarketDataQualityError("at least one symbol is required")
        if len(requested) != len(set(requested)):
            raise MarketDataQualityError("requested symbols must not contain duplicates")
        missing_types = tuple(
            symbol for symbol in requested if symbol not in self._security_types
        )
        if missing_types:
            raise MarketDataQualityError(
                "mock quote security types are missing for: "
                + ", ".join(missing_types)
            )

        now = self._now()
        quotes = tuple(
            MarketQuote(
                symbol=symbol,
                provider_symbol=symbol,
                exchange=(
                    SecurityExchange.XSHG
                    if symbol.endswith(".SH")
                    else SecurityExchange.XSHE
                ),
                market=TradingMarket.A_SHARE,
                security_type=self._security_types[symbol],
                currency=Currency.CNY,
                source="mock",
                source_time=now,
                received_at=now,
                previous_close=Decimal("10.00"),
                open=Decimal("10.10"),
                high=Decimal("10.50"),
                low=Decimal("9.90"),
                last=Decimal("10.20"),
                volume=1000,
                turnover=Decimal("10200.00"),
                market_phase=phase,
                trading_status=TradingStatus.UNKNOWN,
            )
            for symbol in requested
        )
        return QuoteBatch(
            requested_symbols=requested,
            quotes=quotes,
            returned_count=len(quotes),
            completeness=DataCompleteness.COMPLETE,
            coverage_ratio=Decimal(1),
            source="mock",
            market_phase=phase,
            market_state=QuoteMarketState.UNKNOWN,
            freshness=QuoteFreshness.UNKNOWN_MARKET_STATE,
            requested_at=now,
            completed_at=now,
        )
