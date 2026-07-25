import json
import logging
from datetime import UTC, datetime
from typing import cast

import pytest
from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    JobExecutionEvent,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from market_sentinel.domain.models import JobRunStatus, MarketPhase, TradingMarket
from market_sentinel.jobs import ReportService
from market_sentinel.scheduler import build_scheduler


class StubReportService:
    async def run(self, phase: MarketPhase) -> JobRunStatus:
        raise AssertionError(f"Unexpected execution for {phase.value}")


def build_test_scheduler() -> AsyncIOScheduler:
    service = cast(ReportService, StubReportService())
    return build_scheduler(service)


@pytest.mark.parametrize(
    ("status", "phase", "expected_market"),
    [
        (
            JobRunStatus.COMPLETED,
            MarketPhase.A_SHARE_CLOSE,
            TradingMarket.A_SHARE,
        ),
        (
            JobRunStatus.SKIPPED_NON_TRADING_DAY,
            MarketPhase.KOREA_CLOSE,
            TradingMarket.KOREA,
        ),
    ],
)
def test_scheduler_logs_structured_report_result(
    status: JobRunStatus,
    phase: MarketPhase,
    expected_market: TradingMarket,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = build_test_scheduler()
    event = JobExecutionEvent(
        EVENT_JOB_EXECUTED,
        phase.value,
        "default",
        datetime(2026, 7, 24, 1, tzinfo=UTC),
        retval=status,
    )

    with caplog.at_level(logging.INFO, logger="market_sentinel.scheduler"):
        scheduler._dispatch_event(event)

    assert json.loads(caplog.records[-1].message) == {
        "event": "scheduled_report_finished",
        "status": status.value,
        "phase": phase.value,
        "market": expected_market.value,
    }


def test_scheduler_does_not_log_error_event_as_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = build_test_scheduler()
    event = JobExecutionEvent(
        EVENT_JOB_ERROR,
        MarketPhase.US_CLOSE.value,
        "default",
        datetime(2026, 7, 24, 20, 5, tzinfo=UTC),
        exception=RuntimeError("report failed"),
    )

    with caplog.at_level(logging.INFO, logger="market_sentinel.scheduler"):
        scheduler._dispatch_event(event)

    assert caplog.records == []
