from market_sentinel.domain.models import (
    ActionState,
    MarketBrief,
    MarketSnapshot,
    RiskReport,
)
from market_sentinel.llm.base import LLMAnalyst


class MockAnalyst(LLMAnalyst):
    async def analyze(
        self,
        snapshot: MarketSnapshot,
        risk_report: RiskReport,
    ) -> MarketBrief:
        risks = [v.message for v in risk_report.violations]
        return MarketBrief(
            title=f"{snapshot.phase.value} 开发环境简报",
            generated_at=snapshot.generated_at,
            phase=snapshot.phase,
            facts=snapshot.facts,
            inferences=["当前使用 Mock 行情与 Mock 模型，不能据此交易。"],
            counterarguments=["缺少真实行情、新闻和公告数据。"],
            portfolio_risks=risks,
            watch_items=["接入真实数据源后再运行影子模式。"],
            action_state=(
                ActionState.REVIEW_RISK if risks else ActionState.NO_ACTION
            ),
            confidence=0.1,
            disclaimer="仅供开发测试；任何交易由用户独立判断并人工执行。",
        )
