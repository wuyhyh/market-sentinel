from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from market_sentinel.domain.models import TradingMarket

CanonicalSymbol = Annotated[str, Field(pattern=r"^\d{6}\.(SH|SZ)$")]
PositivePrice = Annotated[Decimal, Field(gt=0)]
NonNegativeTurnover = Annotated[Decimal, Field(ge=0)]

_SYMBOL_PATTERN = re.compile(r"^(?P<code>\d{6})\.(?P<suffix>SH|SZ)$")


class DataCompleteness(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class SecurityCategory(StrEnum):
    STOCK = "stock"
    ETF = "etf"
    INDEX = "index"


class SecurityExchange(StrEnum):
    XSHG = "XSHG"
    XSHE = "XSHE"


class Currency(StrEnum):
    CNY = "CNY"


class ListStatus(StrEnum):
    LISTED = "listed"
    PAUSED = "paused"
    DELISTED = "delisted"


class VolumeUnit(StrEnum):
    SHARE = "share"
    LOT = "lot"


class PriceUnit(StrEnum):
    CNY_PER_SECURITY = "CNY_per_security"
    INDEX_POINT = "index_point"


class TurnoverUnit(StrEnum):
    CNY = "CNY"
    CNY_THOUSAND = "CNY_thousand"


class AdjustmentMode(StrEnum):
    NONE = "none"
    FORWARD = "forward"
    BACKWARD = "backward"


class MarketDataErrorCategory(StrEnum):
    PROVIDER = "provider_error"
    OPEND_UNAVAILABLE = "opend_unavailable"
    CONNECTION_REFUSED = "connection_refused"
    AUTHENTICATION_FAILED = "authentication_failed"
    PERMISSION_DENIED = "permission_denied"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limited"
    AUTHORIZATION = "authorization"
    INVALID_RESPONSE = "invalid_response"
    PROTOCOL = "protocol"
    QUALITY = "quality"
    UNSUPPORTED = "unsupported"
    UNEXPECTED = "unexpected_error"


class ProviderError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: MarketDataErrorCategory
    message: Annotated[str, Field(min_length=1)]
    symbol: CanonicalSymbol | None = None
    code: Annotated[str, Field(min_length=1)] | None = None

    @field_validator("message", "code")
    @classmethod
    def reject_blank_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("value must not be blank")
        return value


class SecurityMasterRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: CanonicalSymbol
    name: Annotated[str, Field(min_length=1)]
    market: TradingMarket
    exchange: SecurityExchange
    security_type: SecurityCategory
    currency: Currency | None
    list_status: ListStatus | None
    list_date: date | None = None
    source: Annotated[str, Field(min_length=1)]
    provider_symbol: Annotated[str, Field(min_length=1)] | None = None
    received_at: datetime

    @field_validator("name", "source", "provider_symbol")
    @classmethod
    def reject_blank_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("received_at")
    @classmethod
    def require_aware_received_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("received_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_a_share_identity(self) -> Self:
        if self.market is not TradingMarket.A_SHARE:
            raise ValueError("security master currently supports only the A-share market")
        if self.security_type is not SecurityCategory.INDEX:
            if self.currency is None:
                raise ValueError("stock and ETF records must include currency")
            if self.list_status is None:
                raise ValueError("stock and ETF records must include list_status")

        match = _SYMBOL_PATTERN.fullmatch(self.symbol)
        if match is None:
            raise ValueError("symbol must use the canonical NNNNNN.SH or NNNNNN.SZ form")

        expected_exchange = (
            SecurityExchange.XSHG
            if match.group("suffix") == "SH"
            else SecurityExchange.XSHE
        )
        if self.exchange is not expected_exchange:
            raise ValueError("exchange must match the canonical symbol suffix")
        return self


class DailyBar(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: CanonicalSymbol
    trade_date: Annotated[date, Field(strict=True)]
    source: Annotated[str, Field(min_length=1)]
    received_at: datetime
    previous_close: PositivePrice
    open: PositivePrice
    high: PositivePrice
    low: PositivePrice
    close: PositivePrice
    price_unit: PriceUnit
    volume: Annotated[int, Field(ge=0)]
    turnover: NonNegativeTurnover
    volume_unit: VolumeUnit
    turnover_unit: TurnoverUnit
    adjustment: AdjustmentMode = AdjustmentMode.NONE

    @field_validator("source")
    @classmethod
    def reject_blank_source(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source must not be blank")
        return value

    @field_validator("received_at")
    @classmethod
    def require_aware_received_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("received_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_ohlc(self) -> Self:
        if self.high < self.low:
            raise ValueError("high must be greater than or equal to low")
        if not self.low <= self.open <= self.high:
            raise ValueError("open must be within the low/high range")
        if not self.low <= self.close <= self.high:
            raise ValueError("close must be within the low/high range")
        return self


class _BatchBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requested_symbols: tuple[CanonicalSymbol, ...]
    missing_symbols: tuple[CanonicalSymbol, ...] = ()
    unsupported_symbols: tuple[CanonicalSymbol, ...] = ()
    invalid_symbols: tuple[CanonicalSymbol, ...] = ()
    provider_errors: tuple[ProviderError, ...] = ()
    completeness: DataCompleteness
    source: Annotated[str, Field(min_length=1)]
    requested_at: datetime
    completed_at: datetime

    @field_validator(
        "requested_symbols",
        "missing_symbols",
        "unsupported_symbols",
        "invalid_symbols",
    )
    @classmethod
    def require_unique_sorted_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("symbol lists must not contain duplicates")
        return tuple(sorted(value))

    @field_validator("provider_errors")
    @classmethod
    def sort_provider_errors(cls, value: tuple[ProviderError, ...]) -> tuple[ProviderError, ...]:
        return tuple(
            sorted(
                value,
                key=lambda error: (
                    error.symbol or "",
                    error.category.value,
                    error.code or "",
                    error.message,
                ),
            )
        )

    @field_validator("source")
    @classmethod
    def reject_blank_source(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source must not be blank")
        return value

    @field_validator("requested_at", "completed_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("batch timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_common_batch_invariants(self) -> Self:
        requested = set(self.requested_symbols)
        missing = set(self.missing_symbols)
        unsupported = set(self.unsupported_symbols)
        invalid = set(self.invalid_symbols)

        if not requested:
            raise ValueError("requested_symbols must not be empty")
        if self.completed_at < self.requested_at:
            raise ValueError("completed_at must not be earlier than requested_at")
        if not missing <= requested or not unsupported <= requested or not invalid <= requested:
            raise ValueError("batch issue symbols must be requested symbols")
        if missing & unsupported or missing & invalid or unsupported & invalid:
            raise ValueError("batch issue symbol categories must not overlap")
        for error in self.provider_errors:
            if error.symbol is not None and error.symbol not in requested:
                raise ValueError("provider error symbol must be a requested symbol")
        return self


class SecurityMasterBatch(_BatchBase):
    records: tuple[SecurityMasterRecord, ...]

    @field_validator("records")
    @classmethod
    def require_unique_sorted_records(
        cls,
        value: tuple[SecurityMasterRecord, ...],
    ) -> tuple[SecurityMasterRecord, ...]:
        symbols = [record.symbol for record in value]
        if len(symbols) != len(set(symbols)):
            raise ValueError("security master records must not contain duplicate symbols")
        return tuple(sorted(value, key=lambda record: record.symbol))

    @model_validator(mode="after")
    def validate_completeness(self) -> Self:
        _validate_batch_completeness(
            requested_symbols=self.requested_symbols,
            data_symbols=tuple(record.symbol for record in self.records),
            missing_symbols=self.missing_symbols,
            unsupported_symbols=self.unsupported_symbols,
            invalid_symbols=self.invalid_symbols,
            provider_errors=self.provider_errors,
            completeness=self.completeness,
        )
        for record in self.records:
            if record.source != self.source:
                raise ValueError("record source must match batch source")
        return self


class DailyBarBatch(_BatchBase):
    bars: tuple[DailyBar, ...]

    @field_validator("bars")
    @classmethod
    def require_unique_sorted_bars(cls, value: tuple[DailyBar, ...]) -> tuple[DailyBar, ...]:
        identities = [(bar.symbol, bar.trade_date) for bar in value]
        if len(identities) != len(set(identities)):
            raise ValueError("daily bars must not contain duplicate symbol/date pairs")
        return tuple(sorted(value, key=lambda bar: (bar.symbol, bar.trade_date)))

    @model_validator(mode="after")
    def validate_completeness(self) -> Self:
        _validate_batch_completeness(
            requested_symbols=self.requested_symbols,
            data_symbols=tuple(bar.symbol for bar in self.bars),
            missing_symbols=self.missing_symbols,
            unsupported_symbols=self.unsupported_symbols,
            invalid_symbols=self.invalid_symbols,
            provider_errors=self.provider_errors,
            completeness=self.completeness,
        )
        for bar in self.bars:
            if bar.source != self.source:
                raise ValueError("bar source must match batch source")
        return self


def _validate_batch_completeness(
    *,
    requested_symbols: tuple[str, ...],
    data_symbols: tuple[str, ...],
    missing_symbols: tuple[str, ...],
    unsupported_symbols: tuple[str, ...],
    invalid_symbols: tuple[str, ...],
    provider_errors: tuple[ProviderError, ...],
    completeness: DataCompleteness,
) -> None:
    requested = set(requested_symbols)
    data = set(data_symbols)
    missing = set(missing_symbols)
    unsupported = set(unsupported_symbols)
    invalid = set(invalid_symbols)
    has_declared_issue = bool(missing or unsupported or invalid or provider_errors)
    has_incomplete_result = bool(has_declared_issue or requested - data)

    if not data <= requested:
        raise ValueError("batch data must contain only requested symbols")
    if data & (missing | unsupported | invalid):
        raise ValueError("a returned symbol cannot also have a batch issue")

    if completeness is DataCompleteness.COMPLETE:
        if data != requested or has_incomplete_result:
            raise ValueError("complete batches must cover every request without errors")
    elif completeness is DataCompleteness.PARTIAL:
        if not data or not has_incomplete_result:
            raise ValueError("partial batches require both usable data and an incomplete result")
    elif completeness is DataCompleteness.FAILED:
        if data:
            raise ValueError("failed batches must not contain usable data")
        if not has_declared_issue:
            raise ValueError("failed batches must describe at least one failure")
