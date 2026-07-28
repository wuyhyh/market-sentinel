from datetime import UTC, datetime

from market_sentinel.domain.models import (
    ActionState,
    Position,
    RiskPolicy,
    RiskReport,
    RiskViolation,
)
from market_sentinel.domain.quotes import QuoteBatch
from market_sentinel.domain.security_data import DataCompleteness


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


def evaluate_shadow_market_data_risk(batch: QuoteBatch) -> dict[str, object]:
    """Evaluate replay data safety without inventing portfolio exposures."""
    warnings: list[dict[str, str | None]] = []
    if batch.completeness is DataCompleteness.FAILED:
        warnings.append(
            {
                "code": "MARKET_DATA_FAILED",
                "severity": "critical",
                "message": "The replay contains no usable market quotes.",
                "symbol": None,
            }
        )
    elif batch.completeness is DataCompleteness.PARTIAL:
        warnings.append(
            {
                "code": "MARKET_DATA_PARTIAL",
                "severity": "high",
                "message": "The replay contains an explicitly partial quote batch.",
                "symbol": None,
            }
        )
    warnings.extend(
        {
            "code": "CRITICAL_QUOTE_MISSING",
            "severity": "critical",
            "message": "A critical holding quote is unavailable in the replay.",
            "symbol": symbol,
        }
        for symbol in batch.critical_missing_symbols
    )
    return {
        "action": ActionState.NO_ACTION.value,
        "status": (
            "failed"
            if batch.completeness is DataCompleteness.FAILED
            else "warning"
            if warnings
            else "passed"
        ),
        "warnings": warnings,
        "portfolio_exposure_evaluated": False,
        "reason": "position quantities, costs, and market values are not part of the watchlist",
    }
