from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from market_sentinel.domain.models import TradingMarket
from market_sentinel.domain.security_data import (
    AdjustmentMode,
    Currency,
    DailyBar,
    DailyBarBatch,
    DataCompleteness,
    ListStatus,
    MarketDataErrorCategory,
    ProviderError,
    SecurityCategory,
    SecurityExchange,
    SecurityMasterBatch,
    SecurityMasterRecord,
    TurnoverUnit,
    VolumeUnit,
)

RECEIVED_AT = datetime(2026, 7, 24, 8, 0, tzinfo=UTC)
REQUESTED_AT = RECEIVED_AT - timedelta(seconds=1)


def security_record_data(
    *,
    symbol: str = "600183.SH",
    exchange: SecurityExchange = SecurityExchange.XSHG,
    security_type: SecurityCategory = SecurityCategory.STOCK,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "name": "生益科技",
        "market": TradingMarket.A_SHARE,
        "exchange": exchange,
        "security_type": security_type,
        "currency": Currency.CNY,
        "list_status": ListStatus.LISTED,
        "list_date": date(1998, 10, 28),
        "source": "fixture",
        "provider_symbol": "600183.SH",
        "received_at": RECEIVED_AT,
    }


def daily_bar_data(
    *,
    symbol: str = "600183.SH",
    trade_date: date | str = date(2026, 7, 24),
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "trade_date": trade_date,
        "source": "fixture",
        "received_at": RECEIVED_AT,
        "previous_close": Decimal("55.10"),
        "open": Decimal("55.20"),
        "high": Decimal("56.30"),
        "low": Decimal("54.90"),
        "close": Decimal("56.00"),
        "volume": 123_400,
        "turnover": Decimal("6853200.50"),
        "volume_unit": VolumeUnit.SHARE,
        "turnover_unit": TurnoverUnit.CNY,
        "adjustment": AdjustmentMode.NONE,
    }


def test_valid_security_master_record_supports_provider_independent_fields() -> None:
    record = SecurityMasterRecord.model_validate(
        security_record_data(security_type=SecurityCategory.INDEX)
    )

    assert record.symbol == "600183.SH"
    assert record.security_type is SecurityCategory.INDEX
    assert record.provider_symbol == "600183.SH"
    assert record.model_dump().keys() == {
        "symbol",
        "name",
        "market",
        "exchange",
        "security_type",
        "currency",
        "list_status",
        "list_date",
        "source",
        "provider_symbol",
        "received_at",
    }


def test_security_master_exchange_must_match_symbol_suffix() -> None:
    with pytest.raises(ValidationError, match="exchange must match"):
        SecurityMasterRecord.model_validate(
            security_record_data(exchange=SecurityExchange.XSHE)
        )


def test_valid_unadjusted_daily_bar() -> None:
    bar = DailyBar.model_validate(daily_bar_data())

    assert bar.close == Decimal("56.00")
    assert bar.volume == 123_400
    assert bar.turnover == Decimal("6853200.50")
    assert bar.adjustment is AdjustmentMode.NONE


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (field, value)
        for field in ("previous_close", "open", "high", "low", "close")
        for value in (Decimal(0), Decimal("-0.01"))
    ],
)
def test_daily_bar_rejects_non_positive_prices(
    field: str,
    value: Decimal,
) -> None:
    payload = daily_bar_data()
    payload[field] = value

    with pytest.raises(ValidationError):
        DailyBar.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("high", Decimal("54.00"), "high must be greater"),
        ("open", Decimal("57.00"), "open must be within"),
        ("close", Decimal("54.00"), "close must be within"),
    ],
)
def test_daily_bar_rejects_invalid_ohlc_relationships(
    field: str,
    value: Decimal,
    message: str,
) -> None:
    payload = daily_bar_data()
    payload[field] = value

    with pytest.raises(ValidationError, match=message):
        DailyBar.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("volume", -1),
        ("turnover", Decimal("-0.01")),
    ],
)
def test_daily_bar_rejects_negative_volume_or_turnover(field: str, value: object) -> None:
    payload = daily_bar_data()
    payload[field] = value

    with pytest.raises(ValidationError):
        DailyBar.model_validate(payload)


def test_daily_bar_rejects_invalid_trade_date() -> None:
    with pytest.raises(ValidationError):
        DailyBar.model_validate(daily_bar_data(trade_date="2026-02-30"))


def test_index_daily_bar_uses_the_same_provider_independent_daily_model() -> None:
    bar = DailyBar.model_validate(
        daily_bar_data(symbol="000001.SH"),
    )

    assert bar.symbol == "000001.SH"
    assert bar.trade_date == date(2026, 7, 24)
    assert "source_time" not in DailyBar.model_fields


def test_daily_bar_converts_string_numbers_to_decimal_fields() -> None:
    payload = daily_bar_data()
    for field in ("previous_close", "open", "high", "low", "close", "turnover"):
        payload[field] = str(payload[field])

    bar = DailyBar.model_validate(payload)

    assert isinstance(bar.previous_close, Decimal)
    assert isinstance(bar.open, Decimal)
    assert isinstance(bar.high, Decimal)
    assert isinstance(bar.low, Decimal)
    assert isinstance(bar.close, Decimal)
    assert isinstance(bar.turnover, Decimal)
    assert bar.close == Decimal("56.00")
    assert bar.turnover == Decimal("6853200.50")


def test_missing_ordinary_symbol_produces_partial_security_master_batch() -> None:
    record = SecurityMasterRecord.model_validate(security_record_data())
    batch = SecurityMasterBatch(
        requested_symbols=("000333.SZ", "600183.SH"),
        records=(record,),
        missing_symbols=("000333.SZ",),
        completeness=DataCompleteness.PARTIAL,
        source="fixture",
        requested_at=REQUESTED_AT,
        completed_at=RECEIVED_AT,
    )

    assert batch.completeness is DataCompleteness.PARTIAL
    assert batch.missing_symbols == ("000333.SZ",)
    assert tuple(record.symbol for record in batch.records) == ("600183.SH",)


def test_unsupported_security_produces_partial_batch() -> None:
    record = SecurityMasterRecord.model_validate(security_record_data())
    batch = SecurityMasterBatch(
        requested_symbols=("510300.SH", "600183.SH"),
        records=(record,),
        unsupported_symbols=("510300.SH",),
        completeness=DataCompleteness.PARTIAL,
        source="fixture",
        requested_at=REQUESTED_AT,
        completed_at=RECEIVED_AT,
    )

    assert batch.completeness is DataCompleteness.PARTIAL
    assert batch.unsupported_symbols == ("510300.SH",)
    assert batch.missing_symbols == ()


def test_provider_wide_failure_produces_failed_daily_bar_batch() -> None:
    error = ProviderError(
        category=MarketDataErrorCategory.TIMEOUT,
        message="provider request timed out",
    )
    batch = DailyBarBatch(
        requested_symbols=("600183.SH",),
        bars=(),
        provider_errors=(error,),
        completeness=DataCompleteness.FAILED,
        source="fixture",
        requested_at=REQUESTED_AT,
        completed_at=RECEIVED_AT,
    )

    assert batch.completeness is DataCompleteness.FAILED
    assert batch.bars == ()
    assert batch.provider_errors == (error,)


def test_all_missing_requests_produce_failed_batch() -> None:
    batch = DailyBarBatch(
        requested_symbols=("000001.SH", "600183.SH"),
        bars=(),
        missing_symbols=("600183.SH", "000001.SH"),
        completeness=DataCompleteness.FAILED,
        source="fixture",
        requested_at=REQUESTED_AT,
        completed_at=RECEIVED_AT,
    )

    assert batch.completeness is DataCompleteness.FAILED
    assert batch.missing_symbols == ("000001.SH", "600183.SH")
    assert batch.bars == ()


def test_duplicate_requested_symbols_are_rejected() -> None:
    record = SecurityMasterRecord.model_validate(security_record_data())

    with pytest.raises(ValidationError, match="must not contain duplicates"):
        SecurityMasterBatch(
            requested_symbols=("600183.SH", "600183.SH"),
            records=(record,),
            completeness=DataCompleteness.COMPLETE,
            source="fixture",
            requested_at=REQUESTED_AT,
            completed_at=RECEIVED_AT,
        )


def test_duplicate_security_master_records_are_rejected() -> None:
    record = SecurityMasterRecord.model_validate(security_record_data())

    with pytest.raises(ValidationError, match="duplicate symbols"):
        SecurityMasterBatch(
            requested_symbols=("600183.SH",),
            records=(record, record),
            completeness=DataCompleteness.COMPLETE,
            source="fixture",
            requested_at=REQUESTED_AT,
            completed_at=RECEIVED_AT,
        )


def test_duplicate_daily_symbol_date_pairs_are_rejected() -> None:
    bar = DailyBar.model_validate(daily_bar_data())

    with pytest.raises(ValidationError, match="duplicate symbol/date pairs"):
        DailyBarBatch(
            requested_symbols=("600183.SH",),
            bars=(bar, bar),
            completeness=DataCompleteness.COMPLETE,
            source="fixture",
            requested_at=REQUESTED_AT,
            completed_at=RECEIVED_AT,
        )


def test_batch_output_is_stable_for_different_input_order() -> None:
    sh_record = SecurityMasterRecord.model_validate(security_record_data())
    sz_record = SecurityMasterRecord.model_validate(
        {
            **security_record_data(
                symbol="000333.SZ",
                exchange=SecurityExchange.XSHE,
            ),
            "name": "美的集团",
            "provider_symbol": "000333.SZ",
        }
    )
    first = SecurityMasterBatch(
        requested_symbols=("600183.SH", "000333.SZ"),
        records=(sh_record, sz_record),
        completeness=DataCompleteness.COMPLETE,
        source="fixture",
        requested_at=REQUESTED_AT,
        completed_at=RECEIVED_AT,
    )
    second = SecurityMasterBatch(
        requested_symbols=("000333.SZ", "600183.SH"),
        records=(sz_record, sh_record),
        completeness=DataCompleteness.COMPLETE,
        source="fixture",
        requested_at=REQUESTED_AT,
        completed_at=RECEIVED_AT,
    )

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.requested_symbols == ("000333.SZ", "600183.SH")
    assert tuple(record.symbol for record in first.records) == (
        "000333.SZ",
        "600183.SH",
    )


def test_daily_bar_output_is_stably_sorted_by_symbol_and_date() -> None:
    newer = DailyBar.model_validate(daily_bar_data())
    older = DailyBar.model_validate(daily_bar_data(trade_date=date(2026, 7, 23)))
    batch = DailyBarBatch(
        requested_symbols=("600183.SH",),
        bars=(newer, older),
        completeness=DataCompleteness.COMPLETE,
        source="fixture",
        requested_at=REQUESTED_AT,
        completed_at=RECEIVED_AT,
    )

    assert tuple(bar.trade_date for bar in batch.bars) == (
        date(2026, 7, 23),
        date(2026, 7, 24),
    )


def test_domain_models_reject_dataframe_like_objects_and_raw_payloads() -> None:
    class FakeDataFrame:
        columns = ("ts_code", "name")

    with pytest.raises(ValidationError):
        SecurityMasterRecord.model_validate(FakeDataFrame())

    with pytest.raises(ValidationError, match="raw_payload"):
        SecurityMasterRecord.model_validate(
            {
                **security_record_data(),
                "raw_payload": {"ts_code": "600183.SH"},
            }
        )


def test_trade_date_cannot_be_supplied_as_intraday_source_time() -> None:
    payload = daily_bar_data()
    payload["source_time"] = datetime(2026, 7, 24, 7, 0, tzinfo=UTC)

    with pytest.raises(ValidationError, match="source_time"):
        DailyBar.model_validate(payload)

    assert "source_time" not in DailyBar.model_fields

    payload_without_source_time = daily_bar_data()
    payload_without_source_time["trade_date"] = datetime(2026, 7, 24, 0, 0, tzinfo=UTC)
    with pytest.raises(ValidationError, match="trade_date"):
        DailyBar.model_validate(payload_without_source_time)
