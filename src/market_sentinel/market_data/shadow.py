from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from market_sentinel.config import Settings, get_settings
from market_sentinel.domain.models import MarketPhase
from market_sentinel.domain.quotes import QuoteBatch, QuoteFreshness, QuoteMarketState
from market_sentinel.domain.security_data import (
    DataCompleteness,
    MarketDataErrorCategory,
    ProviderError,
    SecurityCategory,
)
from market_sentinel.domain.watchlist import SecurityRole, SecurityType, WatchlistConfig
from market_sentinel.market_data.base import QuoteMarketDataProvider
from market_sentinel.market_data.mock import MockMarketDataProvider
from market_sentinel.market_data.opend import (
    CRITICAL_HOLDING_SYMBOLS,
    build_opend_market_data_provider,
    sanitize_opend_error,
    sanitize_opend_market_state,
)
from market_sentinel.watchlist import WatchlistConfigurationError, WatchlistLoader

SNAPSHOT_OUTPUT_DIR = Path("data/market-data/snapshots")
SHADOW_REPORT_SCHEMA_VERSION = 1
SUPPORTED_SHADOW_PROVIDERS = frozenset({"mock", "opend"})

ProviderBuilder = Callable[
    [str, Settings, Mapping[str, SecurityCategory]],
    QuoteMarketDataProvider,
]
Clock = Callable[[], datetime]


class ShadowConfigurationError(Exception):
    pass


class ShadowOutputError(Exception):
    pass


def build_shadow_provider(
    provider_name: str,
    settings: Settings,
    security_types: Mapping[str, SecurityCategory],
) -> QuoteMarketDataProvider:
    if provider_name == "mock":
        return MockMarketDataProvider(security_types)
    if provider_name == "opend":
        return build_opend_market_data_provider(settings, security_types)
    raise ShadowConfigurationError(f"unsupported market-data provider: {provider_name}")


async def run_market_data_shadow_command(
    args: argparse.Namespace,
    *,
    settings_loader: Callable[[], Settings] = get_settings,
    provider_builder: ProviderBuilder = build_shadow_provider,
    watchlist_loader: WatchlistLoader | None = None,
    output_dir: Path | None = None,
    now: Clock = lambda: datetime.now(UTC),
) -> int:
    dry_run = bool(args.dry_run)
    settings: Settings | None = None
    if dry_run:
        provider_name = str(args.provider or "mock")
        config_path = args.config or Path(
            os.environ.get("WATCHLIST_CONFIG_PATH", "config/watchlist.yaml")
        )
    else:
        try:
            settings = settings_loader()
        except Exception:  # noqa: BLE001 - settings validation errors vary.
            _print_json(
                _failure_summary(
                    provider=str(args.provider or "mock"),
                    category="configuration",
                    requested_count=0,
                )
            )
            return 2
        provider_name = str(args.provider or settings.market_data_provider)
        config_path = args.config or settings.watchlist_config_path

    if provider_name not in SUPPORTED_SHADOW_PROVIDERS:
        _print_json(
            _failure_summary(
                provider=provider_name,
                category="configuration",
                requested_count=0,
            )
        )
        return 2

    loader = watchlist_loader or WatchlistLoader()
    try:
        watchlist = loader.load(config_path)
    except WatchlistConfigurationError as error:
        _print_json(
            _failure_summary(
                provider=provider_name,
                category="configuration",
                requested_count=0,
                configuration_error_count=len(error.issues),
            )
        )
        return 2

    selected = tuple(security for security in watchlist.securities if security.enabled)
    if not selected:
        _print_json(
            _failure_summary(
                provider=provider_name,
                category="quality",
                requested_count=0,
            )
        )
        return 2

    symbols = tuple(security.symbol for security in selected)
    security_types = {
        security.symbol: _security_category(security.security_type)
        for security in selected
    }
    phase = MarketPhase(args.phase)
    effective_output_dir = output_dir or SNAPSHOT_OUTPUT_DIR

    if dry_run:
        _print_json(
            _dry_run_summary(
                provider_name=provider_name,
                watchlist=watchlist,
                selected_symbols=symbols,
                output_dir=effective_output_dir,
            )
        )
        return 0

    if settings is None:
        raise RuntimeError("settings must be loaded before executing a shadow snapshot")
    try:
        provider = provider_builder(provider_name, settings, security_types)
    except Exception as error:  # noqa: BLE001 - provider configuration errors vary.
        _print_json(
            _failure_summary(
                provider=provider_name,
                category=_exception_category(error),
                requested_count=len(symbols),
            )
        )
        return 2

    requested_at = _utc_now(now)
    try:
        batch = await provider.get_quotes(symbols, phase)
    except Exception as error:  # noqa: BLE001 - provider implementations are pluggable.
        batch = _exception_batch(
            provider_name=provider_name,
            symbols=symbols,
            phase=phase,
            requested_at=requested_at,
            completed_at=_utc_now(now),
            error=error,
        )

    report = build_shadow_report(provider_name, batch)
    try:
        output_path = write_shadow_report_atomic(
            effective_output_dir,
            provider_name,
            batch.completed_at,
            report,
        )
    except (OSError, ShadowOutputError):
        summary = build_shadow_summary(provider_name, batch, output_path=None)
        summary["status"] = "failed"
        error_counts = Counter(
            error.category.value for error in batch.provider_errors
        )
        error_counts["output_write_failed"] += 1
        summary["provider_error_counts"] = dict(sorted(error_counts.items()))
        _print_json(summary)
        return 2

    summary = build_shadow_summary(provider_name, batch, output_path=output_path)
    _print_json(summary)
    return 2 if batch.completeness is DataCompleteness.FAILED else 0


def build_shadow_summary(
    provider_name: str,
    batch: QuoteBatch,
    *,
    output_path: Path | None,
) -> dict[str, object]:
    source_times = tuple(quote.source_time for quote in batch.quotes)
    delays_ms = tuple(quote.delay_seconds * Decimal(1000) for quote in batch.quotes)
    summary: dict[str, object] = {
        "status": batch.completeness.value,
        "provider": provider_name,
        "requested_count": len(batch.requested_symbols),
        "returned_count": batch.returned_count,
        "valid_quote_count": len(batch.quotes),
        "invalid_quote_count": len(batch.invalid_symbols),
        "missing_count": len(batch.missing_symbols),
        "duplicate_count": len(batch.duplicate_symbols),
        "unexpected_count": len(batch.unexpected_symbols),
        "critical_missing_symbols": list(batch.critical_missing_symbols),
        "completeness": batch.completeness.value,
        "market_state": batch.market_state.value,
        "freshness_status": batch.freshness.value,
        "oldest_source_time": min(source_times).isoformat() if source_times else None,
        "newest_source_time": max(source_times).isoformat() if source_times else None,
        "max_delay_ms": _decimal_text(max(delays_ms)) if delays_ms else None,
        "provider_error_counts": dict(
            sorted(Counter(error.category.value for error in batch.provider_errors).items())
        ),
        "snapshot_calls": batch.snapshot_calls,
        "output_path": output_path.as_posix() if output_path else None,
    }
    if any(
        error.code == "missing_optional_dependency"
        for error in batch.provider_errors
    ):
        summary["action_required"] = "install market-sentinel[opend]"
    return summary


def build_shadow_report(
    provider_name: str,
    batch: QuoteBatch,
) -> dict[str, object]:
    safe_provider_errors = [
        {
            "category": error.category.value,
            "code": error.code,
            "message": sanitize_opend_error(error.message),
            "symbol": error.symbol,
        }
        for error in batch.provider_errors
    ]
    quality_issues = [
        {
            "code": issue.code,
            "severity": issue.severity.value,
            "message": sanitize_opend_error(issue.message),
            "symbol": issue.symbol,
        }
        for issue in batch.quality_issues
    ]
    return {
        "schema_version": SHADOW_REPORT_SCHEMA_VERSION,
        "provider": provider_name,
        "requested_at": batch.requested_at.isoformat(),
        "completed_at": batch.completed_at.isoformat(),
        "market_phase": batch.market_phase.value,
        "market_state": batch.market_state.value,
        "raw_market_state": dict(
            sorted(
                Counter(
                    sanitize_opend_market_state(state)
                    for state in batch.raw_market_states
                ).items()
            )
        ),
        "freshness_status": batch.freshness.value,
        "requested_symbols": list(batch.requested_symbols),
        "quotes": [
            quote.model_dump(mode="json")
            for quote in batch.quotes
        ],
        "missing_symbols": list(batch.missing_symbols),
        "stale_symbols": list(batch.stale_symbols),
        "invalid_symbols": list(batch.invalid_symbols),
        "duplicate_symbols": list(batch.duplicate_symbols),
        "unexpected_symbols": list(batch.unexpected_symbols),
        "critical_missing_symbols": list(batch.critical_missing_symbols),
        "provider_errors": safe_provider_errors,
        "quality_gate_result": {
            "status": batch.completeness.value,
            "coverage_ratio": _decimal_text(batch.coverage_ratio),
            "valid_quote_count": len(batch.quotes),
            "invalid_quote_count": len(batch.invalid_symbols),
            "quality_issues": quality_issues,
        },
        "returned_count": batch.returned_count,
        "snapshot_calls": batch.snapshot_calls,
        "market_state_calls": batch.market_state_calls,
        "network_calls": batch.network_calls,
        "completeness": batch.completeness.value,
    }


def write_shadow_report_atomic(
    output_dir: Path,
    provider_name: str,
    completed_at: datetime,
    report: Mapping[str, object],
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = completed_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_path = _non_overwriting_path(
        output_dir,
        f"{timestamp}-{provider_name}",
    )
    serialized = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    json.loads(serialized)

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_dir,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, output_path)
    except OSError as error:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise ShadowOutputError(
            f"unable to write shadow snapshot at {output_path.as_posix()}"
        ) from error
    return output_path


def _dry_run_summary(
    *,
    provider_name: str,
    watchlist: WatchlistConfig,
    selected_symbols: Sequence[str],
    output_dir: Path,
) -> dict[str, object]:
    selected = tuple(
        security
        for security in watchlist.securities
        if security.symbol in set(selected_symbols)
    )
    return {
        "status": "dry_run",
        "provider": provider_name,
        "requested_count": len(selected),
        "stock_count": sum(
            security.security_type is SecurityType.STOCK for security in selected
        ),
        "etf_count": sum(
            security.security_type is SecurityType.ETF for security in selected
        ),
        "index_count": sum(
            security.security_type is SecurityType.INDEX for security in selected
        ),
        "critical_holding_count": sum(
            security.symbol in CRITICAL_HOLDING_SYMBOLS
            and SecurityRole.HOLDING in security.roles
            for security in selected
        ),
        "planned_snapshot_calls": int(provider_name == "opend"),
        "planned_market_state_calls": int(provider_name == "opend"),
        "output_directory": output_dir.as_posix(),
        "network_calls": 0,
    }


def _failure_summary(
    *,
    provider: str,
    category: str,
    requested_count: int,
    configuration_error_count: int | None = None,
) -> dict[str, object]:
    counts = {category: configuration_error_count or 1}
    return {
        "status": "failed",
        "provider": provider,
        "requested_count": requested_count,
        "returned_count": 0,
        "valid_quote_count": 0,
        "invalid_quote_count": 0,
        "missing_count": 0,
        "duplicate_count": 0,
        "unexpected_count": 0,
        "critical_missing_symbols": [],
        "completeness": "failed",
        "market_state": "unknown",
        "freshness_status": "unknown_market_state",
        "oldest_source_time": None,
        "newest_source_time": None,
        "max_delay_ms": None,
        "provider_error_counts": counts,
        "snapshot_calls": 0,
        "output_path": None,
    }


def _exception_batch(
    *,
    provider_name: str,
    symbols: tuple[str, ...],
    phase: MarketPhase,
    requested_at: datetime,
    completed_at: datetime,
    error: BaseException,
) -> QuoteBatch:
    category = _exception_category(error)
    return QuoteBatch(
        requested_symbols=symbols,
        quotes=(),
        provider_errors=(
            ProviderError(
                category=MarketDataErrorCategory.PROVIDER,
                code=category,
                message=sanitize_opend_error(error),
            ),
        ),
        returned_count=0,
        completeness=DataCompleteness.FAILED,
        coverage_ratio=Decimal(0),
        source=provider_name,
        market_phase=phase,
        market_state=QuoteMarketState.UNKNOWN,
        freshness=QuoteFreshness.UNKNOWN_MARKET_STATE,
        requested_at=requested_at,
        completed_at=completed_at,
    )


def _exception_category(error: BaseException) -> str:
    message = str(error).lower()
    if isinstance(error, ImportError) or "optional dependency" in message:
        return "missing_optional_dependency"
    if "permission" in message or "权限" in message:
        return "permission_denied"
    if isinstance(error, TimeoutError) or "timeout" in message:
        return "timeout"
    if "protocol" in message:
        return "protocol_error"
    if isinstance(error, (ShadowConfigurationError, ValueError)):
        return "configuration"
    return "provider_error"


def _security_category(security_type: SecurityType) -> SecurityCategory:
    return {
        SecurityType.STOCK: SecurityCategory.STOCK,
        SecurityType.ETF: SecurityCategory.ETF,
        SecurityType.INDEX: SecurityCategory.INDEX,
    }[security_type]


def _non_overwriting_path(output_dir: Path, stem: str) -> Path:
    candidate = output_dir / f"{stem}.json"
    suffix = 1
    while candidate.exists():
        candidate = output_dir / f"{stem}-{suffix:02d}.json"
        suffix += 1
    return candidate


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _utc_now(clock: Clock) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ShadowConfigurationError("shadow clock must be timezone-aware")
    return value.astimezone(UTC)


def _print_json(payload: Mapping[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
