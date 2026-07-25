from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field


class MarketPhase(StrEnum):
    OVERNIGHT = "overnight"
    KOREA_OPEN = "korea_open"
    A_SHARE_CALL_AUCTION = "a_share_call_auction"
    A_SHARE_OPEN_PRICE = "a_share_open_price"
    A_SHARE_MIDDAY = "a_share_midday"
    KOREA_CLOSE = "korea_close"
    A_SHARE_CLOSE = "a_share_close"
    US_OPEN = "us_open"
    US_CLOSE = "us_close"


class ActionState(StrEnum):
    NO_ACTION = "no_action"
    WATCH = "watch"
    REVIEW_RISK = "review_risk"
    REDUCE_RISK_REVIEW = "reduce_risk_review"


class Fact(BaseModel):
    statement: str
    source: str
    source_time: datetime
    freshness_note: str | None = None


class MarketSnapshot(BaseModel):
    phase: MarketPhase
    generated_at: datetime
    facts: list[Fact]
    raw_metrics: dict[str, float | int | str | None] = Field(default_factory=dict)


class MarketBrief(BaseModel):
    title: str
    generated_at: datetime
    phase: MarketPhase
    facts: list[Fact]
    inferences: list[str] = Field(default_factory=list)
    counterarguments: list[str] = Field(default_factory=list)
    portfolio_risks: list[str] = Field(default_factory=list)
    watch_items: list[str] = Field(default_factory=list)
    action_state: ActionState = ActionState.NO_ACTION
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    disclaimer: str


class Position(BaseModel):
    symbol: str
    name: str
    asset_type: StrEnum | str
    market_value: Annotated[float, Field(ge=0)]
    cost_value: Annotated[float, Field(ge=0)]


class RiskPolicy(BaseModel):
    total_capital: Annotated[float, Field(gt=0)]
    minimum_cash: Annotated[float, Field(ge=0)]
    max_total_stock_value: Annotated[float, Field(ge=0)]
    max_single_stock_value: Annotated[float, Field(ge=0)]
    hard_stop_loss_pct: Annotated[float, Field(gt=0, lt=1)] = 0.05


class RiskViolation(BaseModel):
    code: str
    severity: str
    message: str


class RiskReport(BaseModel):
    checked_at: datetime
    violations: list[RiskViolation]
    metrics: dict[str, float]

    @property
    def passed(self) -> bool:
        return not self.violations
