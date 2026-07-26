from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from market_sentinel.domain.models import TradingMarket
from market_sentinel.domain.security_data import (
    AdjustmentMode,
    Currency,
    DailyBar,
    DailyBarBatch,
    DataCompleteness,
    ListStatus,
    PriceUnit,
    SecurityCategory,
    SecurityExchange,
    SecurityMasterBatch,
    SecurityMasterRecord,
    TurnoverUnit,
    VolumeUnit,
)
from market_sentinel.market_data import (
    DailyBarProvider,
    MarketDataAuthorizationError,
    MarketDataProtocolError,
    MarketDataProviderError,
    MarketDataQualityError,
    MarketDataRateLimitError,
    MarketDataTimeoutError,
    SecurityMasterProvider,
)

RECEIVED_AT = datetime(2026, 7, 24, 8, 0, tzinfo=UTC)


class FakeSecurityMasterProvider(SecurityMasterProvider):
    async def get_security_master(self, symbols: Sequence[str]) -> SecurityMasterBatch:
        records = tuple(
            SecurityMasterRecord(
                symbol=symbol,
                name=f"fixture-{symbol}",
                market=TradingMarket.A_SHARE,
                exchange=(
                    SecurityExchange.XSHG
                    if symbol.endswith(".SH")
                    else SecurityExchange.XSHE
                ),
                security_type=SecurityCategory.STOCK,
                currency=Currency.CNY,
                list_status=ListStatus.LISTED,
                list_date=None,
                source="fake",
                provider_symbol=symbol,
                received_at=RECEIVED_AT,
            )
            for symbol in symbols
        )
        return SecurityMasterBatch(
            requested_symbols=tuple(symbols),
            records=records,
            completeness=DataCompleteness.COMPLETE,
            source="fake",
            requested_at=RECEIVED_AT,
            completed_at=RECEIVED_AT,
        )


class FakeDailyBarProvider(DailyBarProvider):
    async def get_daily_bars(
        self,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> DailyBarBatch:
        assert start_date == end_date
        bars = tuple(
            DailyBar(
                symbol=symbol,
                trade_date=start_date,
                source="fake",
                received_at=RECEIVED_AT,
                previous_close=Decimal("10.00"),
                open=Decimal("10.10"),
                high=Decimal("10.50"),
                low=Decimal("9.90"),
                close=Decimal("10.20"),
                price_unit=PriceUnit.CNY_PER_SECURITY,
                volume=100,
                turnover=Decimal("1020.00"),
                volume_unit=VolumeUnit.SHARE,
                turnover_unit=TurnoverUnit.CNY,
                adjustment=AdjustmentMode.NONE,
            )
            for symbol in symbols
        )
        return DailyBarBatch(
            requested_symbols=tuple(symbols),
            bars=bars,
            completeness=DataCompleteness.COMPLETE,
            source="fake",
            requested_at=RECEIVED_AT,
            completed_at=RECEIVED_AT,
        )


@pytest.mark.asyncio
async def test_fake_security_master_provider_uses_only_domain_types() -> None:
    provider: SecurityMasterProvider = FakeSecurityMasterProvider()

    batch = await provider.get_security_master(("600183.SH", "000333.SZ"))

    assert isinstance(batch, SecurityMasterBatch)
    assert batch.requested_symbols == ("000333.SZ", "600183.SH")
    assert all(isinstance(record, SecurityMasterRecord) for record in batch.records)


@pytest.mark.asyncio
async def test_fake_daily_bar_provider_uses_only_domain_types() -> None:
    provider: DailyBarProvider = FakeDailyBarProvider()
    trade_date = date(2026, 7, 24)

    batch = await provider.get_daily_bars(("600183.SH",), trade_date, trade_date)

    assert isinstance(batch, DailyBarBatch)
    assert batch.bars[0].trade_date == trade_date
    assert not hasattr(batch.bars[0], "source_time")


@pytest.mark.parametrize(
    "error_type",
    [
        MarketDataTimeoutError,
        MarketDataRateLimitError,
        MarketDataAuthorizationError,
        MarketDataProtocolError,
        MarketDataQualityError,
    ],
)
def test_specific_provider_errors_share_the_adr_0003_base_error(
    error_type: type[MarketDataProviderError],
) -> None:
    assert issubclass(error_type, MarketDataProviderError)
