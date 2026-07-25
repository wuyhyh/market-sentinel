from market_sentinel.domain.models import Position, RiskPolicy
from market_sentinel.risk_engine import evaluate_portfolio


def test_portfolio_passes_when_within_limits() -> None:
    policy = RiskPolicy(
        total_capital=500_000,
        minimum_cash=150_000,
        max_total_stock_value=100_000,
        max_single_stock_value=35_000,
        hard_stop_loss_pct=0.05,
    )
    positions = [
        Position(
            symbol="588200.SH",
            name="ETF",
            asset_type="etf_core",
            market_value=150_000,
            cost_value=150_000,
        ),
        Position(
            symbol="CASH.CNY",
            name="现金",
            asset_type="cash",
            market_value=350_000,
            cost_value=350_000,
        ),
    ]

    report = evaluate_portfolio(policy, positions)
    assert report.passed


def test_single_stock_limit_is_enforced() -> None:
    policy = RiskPolicy(
        total_capital=500_000,
        minimum_cash=150_000,
        max_total_stock_value=100_000,
        max_single_stock_value=35_000,
        hard_stop_loss_pct=0.05,
    )
    positions = [
        Position(
            symbol="600000.SH",
            name="示例股票",
            asset_type="individual_stock",
            market_value=40_000,
            cost_value=40_000,
        ),
        Position(
            symbol="CASH.CNY",
            name="现金",
            asset_type="cash",
            market_value=460_000,
            cost_value=460_000,
        ),
    ]

    report = evaluate_portfolio(policy, positions)
    assert not report.passed
    assert any(v.code == "SINGLE_STOCK_EXCEEDED" for v in report.violations)
