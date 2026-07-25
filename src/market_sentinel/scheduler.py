import json
import logging

from apscheduler.events import EVENT_JOB_EXECUTED, JobExecutionEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from market_sentinel.domain.models import JobRunStatus, MarketPhase
from market_sentinel.jobs import ReportService
from market_sentinel.trading_calendar.base import PHASE_MARKETS

logger = logging.getLogger(__name__)


def _log_scheduled_report_result(event: JobExecutionEvent) -> None:
    phase = MarketPhase(event.job_id)
    market = PHASE_MARKETS[phase]
    status = JobRunStatus(event.retval)
    logger.info(
        json.dumps(
            {
                "event": "scheduled_report_finished",
                "status": status.value,
                "phase": phase.value,
                "market": market.value,
            },
            sort_keys=True,
        )
    )


def build_scheduler(service: ReportService) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_listener(_log_scheduled_report_result, EVENT_JOB_EXECUTED)

    # A 股时点（Asia/Shanghai）
    china_jobs = [
        ("overnight", MarketPhase.OVERNIGHT, 8, 35, 0),
        ("a_share_call_auction", MarketPhase.A_SHARE_CALL_AUCTION, 9, 15, 0),
        ("a_share_open_price", MarketPhase.A_SHARE_OPEN_PRICE, 9, 25, 30),
        ("a_share_midday", MarketPhase.A_SHARE_MIDDAY, 11, 35, 0),
        ("korea_close", MarketPhase.KOREA_CLOSE, 14, 32, 0),
        ("a_share_close", MarketPhase.A_SHARE_CLOSE, 15, 5, 0),
    ]

    for job_id, phase, hour, minute, second in china_jobs:
        scheduler.add_job(
            service.run,
            CronTrigger(
                day_of_week="mon-fri",
                hour=hour,
                minute=minute,
                second=second,
                timezone="Asia/Shanghai",
            ),
            args=[phase],
            id=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    # 韩国开盘，按首尔时区定义，避免手工换算。
    scheduler.add_job(
        service.run,
        CronTrigger(
            day_of_week="mon-fri",
            hour=9,
            minute=0,
            second=30,
            timezone="Asia/Seoul",
        ),
        args=[MarketPhase.KOREA_OPEN],
        id="korea_open",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # 美股时点，按纽约时区定义，自动处理夏令时。
    scheduler.add_job(
        service.run,
        CronTrigger(
            day_of_week="mon-fri",
            hour=9,
            minute=30,
            second=30,
            timezone="America/New_York",
        ),
        args=[MarketPhase.US_OPEN],
        id="us_open",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        service.run,
        CronTrigger(
            day_of_week="mon-fri",
            hour=16,
            minute=5,
            second=0,
            timezone="America/New_York",
        ),
        args=[MarketPhase.US_CLOSE],
        id="us_close",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    return scheduler
