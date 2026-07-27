from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from market_sentinel.domain.models import MarketPhase, TradingMarket
from market_sentinel.domain.security_data import (
    Currency,
    DataCompleteness,
    ProviderError,
    SecurityCategory,
    SecurityExchange,
)

CanonicalSymbol = Annotated[str, Field(pattern=r"^\d{6}\.(SH|SZ)$")]
PositiveDecimal = Annotated[Decimal, Field(gt=0)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0)]


class TradingStatus(StrEnum):
    AUCTION = "auction"
    TRADING = "trading"
    SUSPENDED = "suspended"
    HALTED = "halted"
    NO_TRADES = "no_trades"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class QuoteMarketState(StrEnum):
    AUCTION = "auction"
    CONTINUOUS_TRADING = "continuous_trading"
    MIDDAY_BREAK = "midday_break"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class QuoteFreshness(StrEnum):
    OUTSIDE_CONTINUOUS_TRADING = "outside_continuous_trading"
    NOT_VERIFIED_CONTINUOUS_TRADING = "not_verified_continuous_trading"
    UNKNOWN_MARKET_STATE = "unknown_market_state"
    REPLAY = "replay"


class QualitySeverity(StrEnum):
    WARNING = "warning"
    HIGH = "high"
    CRITICAL = "critical"


class QualityIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: Annotated[str, Field(min_length=1)]
    severity: QualitySeverity
    message: Annotated[str, Field(min_length=1)]
    symbol: CanonicalSymbol | None = None


class MarketQuote(BaseModel):
    """A provider-independent, quality-accepted single-security quote."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: CanonicalSymbol
    provider_symbol: Annotated[str, Field(min_length=1)] | None = None
    exchange: SecurityExchange
    market: TradingMarket
    security_type: SecurityCategory
    currency: Currency
    source: Annotated[str, Field(min_length=1)]
    source_time: datetime
    received_at: datetime
    previous_close: PositiveDecimal
    open: PositiveDecimal | None = None
    high: PositiveDecimal | None = None
    low: PositiveDecimal | None = None
    last: PositiveDecimal | None = None
    volume: Annotated[int, Field(ge=0)] | None = None
    turnover: NonNegativeDecimal | None = None
    market_phase: MarketPhase
    trading_status: TradingStatus

    @field_validator("source", "provider_symbol")
    @classmethod
    def reject_blank_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("text fields must not be blank")
        return value

    @field_validator("source_time")
    @classmethod
    def require_aware_source_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("source_time must be timezone-aware")
        return value

    @field_validator("received_at")
    @classmethod
    def normalize_received_at_to_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("received_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_identity_and_prices(self) -> Self:
        if self.market is not TradingMarket.A_SHARE:
            raise ValueError("real-time quotes currently support only A shares")
        if self.currency is not Currency.CNY:
            raise ValueError("A-share quotes must use CNY")
        expected_exchange = (
            SecurityExchange.XSHG
            if self.symbol.endswith(".SH")
            else SecurityExchange.XSHE
        )
        if self.exchange is not expected_exchange:
            raise ValueError("exchange must match the canonical symbol suffix")
        if self.high is not None and self.low is not None:
            if self.high < self.low:
                raise ValueError("high must be greater than or equal to low")
            for field_name, value in (("open", self.open), ("last", self.last)):
                if value is not None and not self.low <= value <= self.high:
                    raise ValueError(f"{field_name} must be within the low/high range")
        return self

    @property
    def delay_seconds(self) -> Decimal:
        delay = self.received_at - self.source_time.astimezone(UTC)
        return Decimal(str(delay.total_seconds()))


class QuoteBatch(BaseModel):
    """A deterministic quote batch after provider adaptation and quality checks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requested_symbols: tuple[CanonicalSymbol, ...]
    quotes: tuple[MarketQuote, ...]
    missing_symbols: tuple[CanonicalSymbol, ...] = ()
    stale_symbols: tuple[CanonicalSymbol, ...] = ()
    invalid_symbols: tuple[CanonicalSymbol, ...] = ()
    duplicate_symbols: tuple[CanonicalSymbol, ...] = ()
    unexpected_symbols: tuple[CanonicalSymbol, ...] = ()
    critical_missing_symbols: tuple[CanonicalSymbol, ...] = ()
    provider_errors: tuple[ProviderError, ...] = ()
    quality_issues: tuple[QualityIssue, ...] = ()
    returned_count: Annotated[int, Field(ge=0)]
    snapshot_calls: Annotated[int, Field(ge=0)] = 0
    market_state_calls: Annotated[int, Field(ge=0)] = 0
    network_calls: Annotated[int, Field(ge=0)] = 0
    completeness: DataCompleteness
    coverage_ratio: Annotated[Decimal, Field(ge=0, le=1)]
    source: Annotated[str, Field(min_length=1)]
    market_phase: MarketPhase
    market_state: QuoteMarketState
    raw_market_states: tuple[str, ...] = ()
    freshness: QuoteFreshness
    requested_at: datetime
    completed_at: datetime

    @field_validator(
        "requested_symbols",
        "missing_symbols",
        "stale_symbols",
        "invalid_symbols",
        "duplicate_symbols",
        "unexpected_symbols",
        "critical_missing_symbols",
    )
    @classmethod
    def sort_unique_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("batch symbol collections must not contain duplicates")
        return tuple(sorted(value))

    @field_validator("quotes")
    @classmethod
    def sort_unique_quotes(cls, value: tuple[MarketQuote, ...]) -> tuple[MarketQuote, ...]:
        symbols = [quote.symbol for quote in value]
        if len(symbols) != len(set(symbols)):
            raise ValueError("quotes must not contain duplicate symbols")
        return tuple(sorted(value, key=lambda quote: quote.symbol))

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

    @field_validator("quality_issues")
    @classmethod
    def sort_quality_issues(cls, value: tuple[QualityIssue, ...]) -> tuple[QualityIssue, ...]:
        return tuple(
            sorted(
                value,
                key=lambda issue: (
                    issue.symbol or "",
                    issue.severity.value,
                    issue.code,
                    issue.message,
                ),
            )
        )

    @field_validator("raw_market_states")
    @classmethod
    def sort_raw_market_states(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not state.strip() for state in value):
            raise ValueError("raw market states must not be blank")
        return tuple(sorted(value))

    @field_validator("requested_at", "completed_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("batch timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_batch(self) -> Self:
        requested = set(self.requested_symbols)
        quote_symbols = {quote.symbol for quote in self.quotes}
        unavailable = (
            set(self.missing_symbols)
            | set(self.stale_symbols)
            | set(self.invalid_symbols)
        )
        if not requested:
            raise ValueError("requested_symbols must not be empty")
        if self.completed_at < self.requested_at:
            raise ValueError("completed_at must not precede requested_at")
        if not quote_symbols <= requested:
            raise ValueError("quotes must contain only requested symbols")
        if quote_symbols & unavailable:
            raise ValueError("accepted quotes cannot also be unavailable")
        if not set(self.duplicate_symbols) <= requested:
            raise ValueError("duplicate symbols must have been requested")
        if not set(self.critical_missing_symbols) <= requested:
            raise ValueError("critical missing symbols must have been requested")
        if set(self.unexpected_symbols) & requested:
            raise ValueError("unexpected symbols must not have been requested")
        expected_coverage = Decimal(len(self.quotes)) / Decimal(len(self.requested_symbols))
        if self.coverage_ratio != expected_coverage:
            raise ValueError("coverage_ratio must equal accepted quotes divided by requests")
        if any(quote.source != self.source for quote in self.quotes):
            raise ValueError("quote source must match batch source")
        if any(quote.market_phase is not self.market_phase for quote in self.quotes):
            raise ValueError("quote market phase must match batch phase")
        if self.network_calls < self.snapshot_calls + self.market_state_calls:
            raise ValueError("network_calls must cover quote endpoint calls")

        has_issues = bool(
            self.missing_symbols
            or self.stale_symbols
            or self.invalid_symbols
            or self.duplicate_symbols
            or self.unexpected_symbols
            or self.provider_errors
        )
        if self.completeness is DataCompleteness.COMPLETE:
            if quote_symbols != requested or has_issues:
                raise ValueError("complete batches must cover all requests without errors")
        elif self.completeness is DataCompleteness.PARTIAL:
            if not self.quotes or not has_issues:
                raise ValueError("partial batches require usable quotes and an issue")
        elif self.completeness is DataCompleteness.FAILED:
            if self.quotes:
                raise ValueError("failed batches must not contain accepted quotes")
            if not has_issues:
                raise ValueError("failed batches must describe at least one failure")
        return self
