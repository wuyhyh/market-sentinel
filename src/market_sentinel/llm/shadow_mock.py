from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from market_sentinel.domain.models import ActionState
from market_sentinel.domain.security_data import DataCompleteness


class ShadowNarrativeInput(BaseModel):
    """Small, quality-gated input; it intentionally excludes full quote rows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    completeness: DataCompleteness
    requested_count: Annotated[int, Field(ge=0)]
    valid_quote_count: Annotated[int, Field(ge=0)]
    advancer_count: Annotated[int, Field(ge=0)]
    decliner_count: Annotated[int, Field(ge=0)]
    unchanged_count: Annotated[int, Field(ge=0)]
    critical_missing_symbols: tuple[str, ...]
    warning_codes: tuple[str, ...]
    risk_action: ActionState


class ShadowNarrative(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: Annotated[str, Field(min_length=1)]
    observations: tuple[Annotated[str, Field(min_length=1)], ...]
    limitations: tuple[Annotated[str, Field(min_length=1)], ...]


class ShadowNarrator(ABC):
    @abstractmethod
    async def generate(self, inputs: ShadowNarrativeInput) -> ShadowNarrative:
        raise NotImplementedError


class MockShadowNarrator(ShadowNarrator):
    """Deterministic offline narration for replay-only shadow reports."""

    async def generate(self, inputs: ShadowNarrativeInput) -> ShadowNarrative:
        return ShadowNarrative(
            summary=(
                "该离线简报描述录制快照时的行情状态，"
                "不代表回放执行时的实时市场。"
            ),
            observations=(
                (
                    f"快照请求 {inputs.requested_count} 只证券，"
                    f"质量门接受 {inputs.valid_quote_count} 条报价。"
                ),
                (
                    f"可计算涨跌的证券中，上涨 {inputs.advancer_count} 只、"
                    f"下跌 {inputs.decliner_count} 只、"
                    f"平盘 {inputs.unchanged_count} 只。"
                ),
                f"原始行情批次完整性为 {inputs.completeness.value}。",
            ),
            limitations=(
                "未提供新闻、公司事件、宏观或行业因果证据。",
                "未提供持仓数量、成本、市值或盈亏数据。",
                "该回放不生成投资建议，也不触发任何交易动作。",
            ),
        )
