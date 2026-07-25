from datetime import UTC, datetime

from market_sentinel.domain.models import (
    Position,
    RiskPolicy,
    RiskReport,
    RiskViolation,
)


def evaluate_portfolio(policy: RiskPolicy, positions: list[Position]) -> RiskReport:
    cash = sum(p.market_value for p in positions if str(p.asset_type) == "cash")
    stocks = [p for p in positions if str(p.asset_type) == "individual_stock"]
    total_stock_value = sum(p.market_value for p in stocks)
    total_position_value = sum(p.market_value for p in positions)

    violations: list[RiskViolation] = []

    if cash < policy.minimum_cash:
        violations.append(
            RiskViolation(
                code="CASH_BELOW_MINIMUM",
                severity="high",
                message=f"现金 {cash:.2f} 低于最低要求 {policy.minimum_cash:.2f}",
            )
        )

    if total_stock_value > policy.max_total_stock_value:
        violations.append(
            RiskViolation(
                code="STOCK_BUCKET_EXCEEDED",
                severity="high",
                message=(
                    f"个股总市值 {total_stock_value:.2f} 超过上限 "
                    f"{policy.max_total_stock_value:.2f}"
                ),
            )
        )

    for position in stocks:
        if position.market_value > policy.max_single_stock_value:
            violations.append(
                RiskViolation(
                    code="SINGLE_STOCK_EXCEEDED",
                    severity="high",
                    message=(
                        f"{position.symbol} 市值 {position.market_value:.2f} 超过单只个股上限 "
                        f"{policy.max_single_stock_value:.2f}"
                    ),
                )
            )

    if total_position_value > policy.total_capital * 1.001:
        violations.append(
            RiskViolation(
                code="CAPITAL_MISMATCH",
                severity="critical",
                message=(
                    f"持仓合计 {total_position_value:.2f} 超过配置总资金 "
                    f"{policy.total_capital:.2f}"
                ),
            )
        )

    return RiskReport(
        checked_at=datetime.now(UTC),
        violations=violations,
        metrics={
            "cash": cash,
            "total_stock_value": total_stock_value,
            "total_position_value": total_position_value,
            "cash_ratio": cash / policy.total_capital,
            "stock_ratio": total_stock_value / policy.total_capital,
        },
    )
