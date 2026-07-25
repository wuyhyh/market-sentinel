from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from market_sentinel.bootstrap import build_report_service
from market_sentinel.config import get_settings
from market_sentinel.domain.models import MarketPhase
from market_sentinel.scheduler import build_scheduler

service = build_report_service()
scheduler = build_scheduler(service)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    if get_settings().enable_scheduler and not scheduler.running:
        scheduler.start()
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(
    title="MarketSentinel",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/jobs/{phase}")
async def run_job(phase: str) -> dict[str, str]:
    try:
        market_phase = MarketPhase(phase)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown phase: {phase}") from exc

    status = await service.run(market_phase)
    return {"status": status.value, "phase": market_phase.value}
