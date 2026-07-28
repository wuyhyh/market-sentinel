from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from market_sentinel.domain.models import ActionState
from market_sentinel.domain.quotes import MarketQuote, QuoteBatch, TradingStatus
from market_sentinel.domain.security_data import DataCompleteness, SecurityCategory
from market_sentinel.domain.watchlist import WatchlistConfig
from market_sentinel.llm.shadow_mock import (
    MockShadowNarrator,
    ShadowNarrativeInput,
    ShadowNarrator,
)
from market_sentinel.market_data.opend import sanitize_opend_error
from market_sentinel.market_data.replay import (
    LoadedMarketSnapshot,
    MarketSnapshotReader,
    SnapshotReadError,
    SnapshotReplayMarketDataProvider,
)
from market_sentinel.market_data.shadow import (
    ShadowOutputError,
    write_shadow_report_atomic,
)
from market_sentinel.risk_engine import evaluate_shadow_market_data_risk
from market_sentinel.watchlist import WatchlistConfigurationError, WatchlistLoader

SHADOW_REPORT_SCHEMA_VERSION = 1
SHADOW_REPORT_OUTPUT_DIR = Path("data/reports/shadow")
_PERCENT_QUANTUM = Decimal("0.0001")

Clock = Callable[[], datetime]


class ShadowReportError(Exception):
    pass


@dataclass(frozen=True)
class ShadowReportRun:
    status: str
    report: dict[str, object]
    output_path: Path


class ShadowReportService:
    """Offline report pipeline backed exclusively by a validated replay provider."""

    def __init__(
        self,
        *,
        provider: SnapshotReplayMarketDataProvider,
        narrator: ShadowNarrator | None = None,
        output_dir: Path = SHADOW_REPORT_OUTPUT_DIR,
        now: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self._provider = provider
        self._narrator = narrator or MockShadowNarrator()
        self._output_dir = output_dir
        self._now = now

    async def run(
        self,
        snapshot: LoadedMarketSnapshot,
        watchlist: WatchlistConfig,
    ) -> ShadowReportRun:
        requested = tuple(
            security.symbol for security in watchlist.securities if security.enabled
        )
        batch = await self._provider.get_quotes(
            requested,
            snapshot.batch.market_phase,
        )
        if batch.network_calls != 0:
            raise ShadowReportError("replay report provider attempted a network call")

        analysis = calculate_market_statistics(batch, watchlist)
        risk_result = evaluate_shadow_market_data_risk(batch)
        warnings = build_report_warnings(batch, analysis)
        llm_status = "skipped_data_failed"
        llm_error: str | None = None
        narrative: dict[str, object] | None = None
        if batch.completeness is not DataCompleteness.FAILED:
            try:
                generated = await self._narrator.generate(
                    build_narrative_input(batch, analysis, risk_result, warnings)
                )
                narrative = generated.model_dump(mode="json")
                llm_status = "completed"
            except Exception as error:  # noqa: BLE001 - injected mock failures are reportable.
                llm_status = "failed"
                llm_error = sanitize_opend_error(error) or type(error).__name__
                warnings.append(
                    {
                        "code": "MOCK_LLM_FAILED",
                        "severity": "high",
                        "message": "Mock narrative generation failed; deterministic results remain.",
                        "symbol": None,
                    }
                )

        generated_at = _utc_now(self._now)
        status = _report_status(batch.completeness, llm_status)
        report = build_shadow_replay_report(
            snapshot=snapshot,
            batch=batch,
            analysis=analysis,
            risk_result=risk_result,
            warnings=warnings,
            narrative=narrative,
            llm_status=llm_status,
            llm_error=llm_error,
            status=status,
            generated_at=generated_at,
        )
        output_path = write_shadow_report_atomic(
            self._output_dir,
            "replay-report",
            generated_at,
            report,
        )
        return ShadowReportRun(status=status, report=report, output_path=output_path)


async def run_shadow_report_command(
    args: argparse.Namespace,
    *,
    reader: MarketSnapshotReader | None = None,
    watchlist_loader: WatchlistLoader | None = None,
    narrator: ShadowNarrator | None = None,
    output_dir: Path | None = None,
    now: Clock = lambda: datetime.now(UTC),
) -> int:
    input_path = Path(args.input)
    config_path = args.config or Path(
        os.environ.get("WATCHLIST_CONFIG_PATH", "config/watchlist.yaml")
    )
    try:
        watchlist = (watchlist_loader or WatchlistLoader()).load(config_path)
    except WatchlistConfigurationError as error:
        _print_json(
            _failure_summary(
                input_path=input_path,
                category="configuration",
                error_count=len(error.issues),
            )
        )
        return 2
    try:
        snapshot = (reader or MarketSnapshotReader()).read(input_path)
    except SnapshotReadError as error:
        _print_json(
            _failure_summary(input_path=input_path, category=error.code)
        )
        return 2

    provider = SnapshotReplayMarketDataProvider(snapshot, now=now)
    service = ShadowReportService(
        provider=provider,
        narrator=narrator,
        output_dir=output_dir or SHADOW_REPORT_OUTPUT_DIR,
        now=now,
    )
    try:
        run = await service.run(snapshot, watchlist)
    except (OSError, ShadowOutputError):
        _print_json(
            _failure_summary(
                input_path=input_path,
                input_provider=snapshot.input_provider,
                category="output_write_failed",
            )
        )
        return 2
    except Exception as error:  # noqa: BLE001 - provider implementations are injectable.
        _print_json(
            _failure_summary(
                input_path=input_path,
                input_provider=snapshot.input_provider,
                category=_exception_category(error),
            )
        )
        return 2

    _print_json(build_shadow_report_summary(run, snapshot))
    return 2 if run.status == "failed" else 0


def calculate_market_statistics(
    batch: QuoteBatch,
    watchlist: WatchlistConfig,
) -> dict[str, object]:
    changes: list[tuple[str, Decimal, Decimal]] = []
    for quote in batch.quotes:
        change = _quote_change(quote)
        if change is not None:
            changes.append((quote.symbol, change[0], change[1]))

    percentages = tuple(item[2] for item in changes)
    maximum_gain = (
        min(changes, key=lambda item: (-item[2], item[0])) if changes else None
    )
    maximum_loss = (
        min(changes, key=lambda item: (item[2], item[0])) if changes else None
    )
    quote_types = Counter(quote.security_type for quote in batch.quotes)
    names = {security.symbol: security.name for security in watchlist.securities}
    quote_map = {quote.symbol: quote for quote in batch.quotes}
    return {
        "requested_count": len(batch.requested_symbols),
        "valid_quote_count": len(batch.quotes),
        "invalid_quote_count": len(batch.invalid_symbols),
        "missing_count": len(batch.missing_symbols),
        "critical_missing_symbols": list(batch.critical_missing_symbols),
        "advancer_count": sum(percent > 0 for percent in percentages),
        "decliner_count": sum(percent < 0 for percent in percentages),
        "unchanged_count": sum(percent == 0 for percent in percentages),
        "unpriced_or_uncalculable_count": len(batch.quotes) - len(changes),
        "average_change_pct": _decimal_or_none(_average(percentages)),
        "median_change_pct": _decimal_or_none(_median(percentages)),
        "maximum_gain_symbol": maximum_gain[0] if maximum_gain else None,
        "maximum_gain_change_pct": (
            _percent_text(maximum_gain[2]) if maximum_gain else None
        ),
        "maximum_loss_symbol": maximum_loss[0] if maximum_loss else None,
        "maximum_loss_change_pct": (
            _percent_text(maximum_loss[2]) if maximum_loss else None
        ),
        "turnover_total": _decimal_text(
            sum(
                (quote.turnover for quote in batch.quotes if quote.turnover is not None),
                Decimal(0),
            )
        ),
        "stock_count": quote_types[SecurityCategory.STOCK],
        "etf_count": quote_types[SecurityCategory.ETF],
        "critical_holdings": [
            _critical_holding_summary(symbol, names.get(symbol), quote_map.get(symbol))
            for symbol in ("510300.SH", "588200.SH", "600183.SH")
        ],
    }


def build_shadow_replay_report(
    *,
    snapshot: LoadedMarketSnapshot,
    batch: QuoteBatch,
    analysis: Mapping[str, object],
    risk_result: Mapping[str, object],
    warnings: Sequence[Mapping[str, object]],
    narrative: Mapping[str, object] | None,
    llm_status: str,
    llm_error: str | None,
    status: str,
    generated_at: datetime,
) -> dict[str, object]:
    return {
        "schema_version": SHADOW_REPORT_SCHEMA_VERSION,
        "report_id": f"shadow-replay-{generated_at.strftime('%Y%m%dT%H%M%SZ')}",
        "status": status,
        "data_mode": "replay",
        "execution_mode": "shadow",
        "input_snapshot_path": snapshot.input_path.as_posix(),
        "input_provider": snapshot.input_provider,
        "original_requested_at": snapshot.original_requested_at.isoformat(),
        "original_completed_at": snapshot.original_completed_at.isoformat(),
        "replayed_at": batch.completed_at.isoformat(),
        "generated_at": generated_at.isoformat(),
        "original_market_state": snapshot.original_market_state.value,
        "original_freshness_status": snapshot.original_freshness.value,
        "completeness": batch.completeness.value,
        "facts": [_quote_fact(quote, snapshot.input_path) for quote in batch.quotes],
        "deterministic_analysis": dict(analysis),
        "risk_result": dict(risk_result),
        "narrative": dict(narrative) if narrative is not None else None,
        "warnings": [dict(warning) for warning in warnings],
        "missing_symbols": list(batch.missing_symbols),
        "stale_symbols": list(batch.stale_symbols),
        "invalid_symbols": list(batch.invalid_symbols),
        "duplicate_symbols": list(batch.duplicate_symbols),
        "unexpected_symbols": list(batch.unexpected_symbols),
        "critical_missing_symbols": list(batch.critical_missing_symbols),
        "provider_errors": [
            {
                "category": error.category.value,
                "code": error.code,
                "message": sanitize_opend_error(error.message),
                "symbol": error.symbol,
            }
            for error in batch.provider_errors
        ],
        "llm_provider": "mock",
        "llm_status": llm_status,
        "llm_error": llm_error,
        "network_calls": 0,
    }


def build_shadow_report_summary(
    run: ShadowReportRun,
    snapshot: LoadedMarketSnapshot,
) -> dict[str, object]:
    analysis = run.report["deterministic_analysis"]
    risk_result = run.report["risk_result"]
    warnings = run.report["warnings"]
    if not isinstance(analysis, Mapping) or not isinstance(risk_result, Mapping):
        raise ShadowReportError("shadow report contains invalid deterministic results")
    if not isinstance(warnings, list):
        raise ShadowReportError("shadow report contains invalid warnings")
    return {
        "status": run.status,
        "execution_mode": "shadow",
        "data_mode": "replay",
        "input_path": snapshot.input_path.as_posix(),
        "input_provider": snapshot.input_provider,
        "completeness": run.report["completeness"],
        "requested_count": analysis["requested_count"],
        "valid_quote_count": analysis["valid_quote_count"],
        "missing_count": analysis["missing_count"],
        "critical_missing_symbols": analysis["critical_missing_symbols"],
        "advancer_count": analysis["advancer_count"],
        "decliner_count": analysis["decliner_count"],
        "unchanged_count": analysis["unchanged_count"],
        "warning_count": len(warnings),
        "risk_action": risk_result["action"],
        "llm_provider": "mock",
        "llm_status": run.report["llm_status"],
        "network_calls": 0,
        "output_path": run.output_path.as_posix(),
    }


def build_narrative_input(
    batch: QuoteBatch,
    analysis: Mapping[str, object],
    risk_result: Mapping[str, object],
    warnings: Sequence[Mapping[str, object]],
) -> ShadowNarrativeInput:
    return ShadowNarrativeInput(
        completeness=batch.completeness,
        requested_count=_int_field(analysis, "requested_count"),
        valid_quote_count=_int_field(analysis, "valid_quote_count"),
        advancer_count=_int_field(analysis, "advancer_count"),
        decliner_count=_int_field(analysis, "decliner_count"),
        unchanged_count=_int_field(analysis, "unchanged_count"),
        critical_missing_symbols=batch.critical_missing_symbols,
        warning_codes=tuple(
            str(warning["code"]) for warning in warnings if "code" in warning
        ),
        risk_action=ActionState(str(risk_result["action"])),
    )


def build_report_warnings(
    batch: QuoteBatch,
    analysis: Mapping[str, object],
) -> list[dict[str, object]]:
    warnings: list[dict[str, object]] = [
        {
            "code": issue.code,
            "severity": issue.severity.value,
            "message": sanitize_opend_error(issue.message),
            "symbol": issue.symbol,
        }
        for issue in batch.quality_issues
    ]
    warnings.extend(
        {
            "code": "MISSING_QUOTE",
            "severity": (
                "critical"
                if symbol in batch.critical_missing_symbols
                else "high"
            ),
            "message": "A requested security has no quote in the replay.",
            "symbol": symbol,
        }
        for symbol in batch.missing_symbols
    )
    warnings.extend(
        {
            "code": "STALE_QUOTE_REJECTED",
            "severity": "high",
            "message": "A stale quote was rejected by deterministic validation.",
            "symbol": symbol,
        }
        for symbol in batch.stale_symbols
    )
    warnings.extend(
        {
            "code": error.code or error.category.value,
            "severity": "critical",
            "message": sanitize_opend_error(error.message),
            "symbol": error.symbol,
        }
        for error in batch.provider_errors
    )
    warnings.extend(
        {
            "code": "QUOTE_CHANGE_UNAVAILABLE",
            "severity": "warning",
            "message": "A quote cannot produce a price-change percentage.",
            "symbol": quote.symbol,
        }
        for quote in batch.quotes
        if _quote_change(quote) is None
    )
    if _int_field(analysis, "invalid_quote_count"):
        warnings.append(
            {
                "code": "INVALID_QUOTES_REJECTED",
                "severity": "high",
                "message": "Invalid quotes were rejected by deterministic validation.",
                "symbol": None,
            }
        )
    return sorted(
        warnings,
        key=lambda warning: (
            str(warning.get("symbol") or ""),
            str(warning["severity"]),
            str(warning["code"]),
        ),
    )


def _quote_fact(quote: MarketQuote, input_path: Path) -> dict[str, object]:
    return {
        "source": quote.source,
        "symbol": quote.symbol,
        "source_time": quote.source_time.isoformat(),
        "received_at": quote.received_at.isoformat(),
        "input_snapshot_path": input_path.as_posix(),
        "previous_close": _decimal_text(quote.previous_close),
        "last_price": _decimal_or_none(quote.last),
        "open": _decimal_or_none(quote.open),
        "high": _decimal_or_none(quote.high),
        "low": _decimal_or_none(quote.low),
        "volume": quote.volume,
        "turnover": _decimal_or_none(quote.turnover),
        "trading_status": quote.trading_status.value,
    }


def _critical_holding_summary(
    symbol: str,
    name: str | None,
    quote: MarketQuote | None,
) -> dict[str, object]:
    if quote is None:
        return {
            "symbol": symbol,
            "name": name,
            "last_price": None,
            "previous_close": None,
            "change": None,
            "change_pct": None,
            "trading_status": None,
            "source_time": None,
        }
    change = _quote_change(quote)
    return {
        "symbol": symbol,
        "name": name,
        "last_price": _decimal_or_none(quote.last),
        "previous_close": _decimal_text(quote.previous_close),
        "change": _decimal_text(change[0]) if change else None,
        "change_pct": _percent_text(change[1]) if change else None,
        "trading_status": quote.trading_status.value,
        "source_time": quote.source_time.isoformat(),
    }


def _quote_change(quote: MarketQuote) -> tuple[Decimal, Decimal] | None:
    previous_close = quote.previous_close
    if (
        quote.last is None
        or previous_close is None
        or previous_close <= 0
        or quote.trading_status in {TradingStatus.SUSPENDED, TradingStatus.NO_TRADES}
    ):
        return None
    change = quote.last - previous_close
    percentage = change / previous_close * Decimal(100)
    return change, percentage


def _average(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return (sum(values, Decimal(0)) / Decimal(len(values))).quantize(
        _PERCENT_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle].quantize(
            _PERCENT_QUANTUM,
            rounding=ROUND_HALF_UP,
        )
    return ((ordered[middle - 1] + ordered[middle]) / Decimal(2)).quantize(
        _PERCENT_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


def _report_status(completeness: DataCompleteness, llm_status: str) -> str:
    if completeness is DataCompleteness.FAILED:
        return "failed"
    if completeness is DataCompleteness.PARTIAL or llm_status == "failed":
        return "partial"
    return "complete"


def _failure_summary(
    *,
    input_path: Path,
    category: str,
    input_provider: str | None = None,
    error_count: int = 1,
) -> dict[str, object]:
    return {
        "status": "failed",
        "execution_mode": "shadow",
        "data_mode": "replay",
        "input_path": input_path.as_posix(),
        "input_provider": input_provider,
        "completeness": "failed",
        "requested_count": 0,
        "valid_quote_count": 0,
        "missing_count": 0,
        "critical_missing_symbols": [],
        "advancer_count": 0,
        "decliner_count": 0,
        "unchanged_count": 0,
        "warning_count": 0,
        "risk_action": ActionState.NO_ACTION.value,
        "llm_provider": "mock",
        "llm_status": "not_started",
        "provider_error_counts": {category: error_count},
        "network_calls": 0,
        "output_path": None,
    }


def _exception_category(error: BaseException) -> str:
    if isinstance(error, ShadowReportError):
        return "quality"
    return "provider_error"


def _int_field(values: Mapping[str, object], key: str) -> int:
    value = values[key]
    if type(value) is not int:
        raise ShadowReportError(f"{key} must be an integer")
    return value


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _percent_text(value: Decimal) -> str:
    return _decimal_text(
        value.quantize(_PERCENT_QUANTUM, rounding=ROUND_HALF_UP)
    )


def _decimal_or_none(value: Decimal | None) -> str | None:
    return _decimal_text(value) if value is not None else None


def _utc_now(clock: Clock) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ShadowReportError("shadow report clock must be timezone-aware")
    return value.astimezone(UTC)


def _print_json(payload: Mapping[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
