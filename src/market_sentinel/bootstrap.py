from pathlib import Path

import yaml

from market_sentinel.config import get_settings
from market_sentinel.domain.models import Position, RiskPolicy
from market_sentinel.jobs import ReportService
from market_sentinel.llm.factory import build_analyst
from market_sentinel.market_data.mock import MockMarketDataProvider
from market_sentinel.notifications.console import ConsoleNotifier
from market_sentinel.notifications.webhook import WebhookNotifier


def build_report_service() -> ReportService:
    settings = get_settings()
    config_path = Path("config/portfolio.example.yaml")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    policy = RiskPolicy(
        total_capital=raw["total_capital"],
        minimum_cash=raw["minimum_cash"],
        max_total_stock_value=raw["max_total_stock_value"],
        max_single_stock_value=raw["max_single_stock_value"],
        hard_stop_loss_pct=raw["hard_stop_loss_pct"],
    )
    positions = [Position.model_validate(item) for item in raw["positions"]]

    notifier = (
        WebhookNotifier(settings.notify_webhook_url)
        if settings.notify_webhook_url
        else ConsoleNotifier()
    )

    return ReportService(
        data_provider=MockMarketDataProvider(),
        analyst=build_analyst(settings),
        notifier=notifier,
        risk_policy=policy,
        positions=positions,
    )
