from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from market_sentinel.domain.models import TradingMarket

SYMBOL_PATTERN = re.compile(r"^[0-9]{6}\.(SH|SZ)$")


class SecurityType(StrEnum):
    STOCK = "stock"
    ETF = "etf"


class SecurityRole(StrEnum):
    HOLDING = "holding"
    WATCH = "watch"


ROLE_ORDER: dict[SecurityRole, int] = {
    SecurityRole.HOLDING: 0,
    SecurityRole.WATCH: 1,
}


class WatchPriority(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"


class AShareExchange(StrEnum):
    SH = "SH"
    SZ = "SZ"


class WatchSecurity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(pattern=SYMBOL_PATTERN.pattern)
    name: str
    market: TradingMarket
    exchange: AShareExchange
    security_type: SecurityType
    enabled: StrictBool = True
    roles: tuple[SecurityRole, ...] = (SecurityRole.WATCH,)
    priority: WatchPriority = WatchPriority.NORMAL
    notes: str | None = None

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{6}", value):
            return value
        if value[0] in {"5", "6"}:
            return f"{value}.SH"
        if value[0] in {"0", "1", "2", "3"}:
            return f"{value}.SZ"
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be empty")
        if value != value.strip():
            raise ValueError("name must not have leading or trailing whitespace")
        return value

    @field_validator("roles")
    @classmethod
    def validate_roles(
        cls, value: tuple[SecurityRole, ...]
    ) -> tuple[SecurityRole, ...]:
        if not value:
            raise ValueError("roles must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("roles must not contain duplicates")
        return tuple(sorted(value, key=ROLE_ORDER.__getitem__))

    @model_validator(mode="after")
    def validate_a_share_identity(self) -> Self:
        if self.market is not TradingMarket.A_SHARE:
            raise ValueError("market must be a_share")

        code, suffix = self.symbol.split(".")
        expected_exchange = (
            AShareExchange.SH
            if code[0] in {"5", "6"}
            else AShareExchange.SZ
            if code[0] in {"0", "1", "2", "3"}
            else None
        )
        if expected_exchange is None:
            raise ValueError("symbol prefix is not supported for the A-share watchlist")
        if suffix != self.exchange.value:
            raise ValueError("symbol suffix must match exchange")
        if self.exchange is not expected_exchange:
            raise ValueError("symbol prefix must match exchange")

        if SecurityRole.HOLDING in self.roles:
            if not self.enabled:
                raise ValueError("holding securities must be enabled")
            if self.priority is not WatchPriority.CRITICAL:
                raise ValueError("holding securities must have critical priority")
        return self


class WatchlistConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    declared_count: int | None = Field(default=None, gt=0)
    securities: tuple[WatchSecurity, ...]

    @field_validator("securities")
    @classmethod
    def validate_and_sort_securities(
        cls, value: tuple[WatchSecurity, ...]
    ) -> tuple[WatchSecurity, ...]:
        if not value:
            raise ValueError("securities must not be empty")
        symbols = [security.symbol for security in value]
        if len(set(symbols)) != len(symbols):
            raise ValueError("securities must not contain duplicate symbols")
        return tuple(sorted(value, key=lambda security: security.symbol))

    @model_validator(mode="after")
    def validate_declared_count(self) -> Self:
        if self.declared_count is not None and self.declared_count != len(self.securities):
            raise ValueError(
                "declared_count does not match the number of securities"
            )
        return self
