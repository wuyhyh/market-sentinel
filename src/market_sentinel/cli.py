import argparse
import asyncio
import json
import os
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from market_sentinel.bootstrap import build_report_service
from market_sentinel.config import Settings, get_settings
from market_sentinel.domain.models import MarketPhase
from market_sentinel.domain.security_data import (
    DailyBarBatch,
    SecurityCategory,
    SecurityMasterBatch,
)
from market_sentinel.domain.watchlist import (
    AShareExchange,
    SecurityRole,
    SecurityType,
    WatchlistConfig,
    WatchPriority,
)
from market_sentinel.market_data import (
    MarketDataAuthorizationError,
    MarketDataProviderError,
    MarketDataQualityError,
    MarketDataRateLimitError,
    MarketDataTimeoutError,
    SecurityMasterCache,
    SecurityMasterCacheEntry,
    SecurityMasterCacheError,
    build_tushare_reference_providers,
)
from market_sentinel.market_data.replay import run_market_data_replay_command
from market_sentinel.market_data.shadow import run_market_data_shadow_command
from market_sentinel.reporting.shadow import run_shadow_report_command
from market_sentinel.watchlist import WatchlistConfigurationError, WatchlistLoader

REFERENCE_OUTPUT_DIR = Path("data/reference/tushare")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="market-sentinel")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_once = subparsers.add_parser("run-once", help="Run one report immediately")
    run_once.add_argument("phase", choices=[item.value for item in MarketPhase])

    watchlist = subparsers.add_parser(
        "watchlist",
        help="Validate or summarize the local A-share watchlist",
    )
    watchlist_commands = watchlist.add_subparsers(
        dest="watchlist_command",
        required=True,
    )
    for command in ("validate", "summary"):
        watchlist_command = watchlist_commands.add_parser(
            command,
            help=f"{command.title()} the local A-share watchlist",
        )
        watchlist_command.add_argument(
            "--config",
            type=Path,
            help="Override WATCHLIST_CONFIG_PATH for this command",
        )

    reference = subparsers.add_parser(
        "reference",
        help="Fetch Tushare security-master or daily reference data",
    )
    reference_commands = reference.add_subparsers(
        dest="reference_command",
        required=True,
    )
    security_master = reference_commands.add_parser(
        "security-master",
        help="Fetch configured A-share security master records",
    )
    _add_reference_common_arguments(security_master)
    security_master.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh the cache from Tushare instead of reading it only",
    )
    security_master.add_argument(
        "--allow-stale",
        action="store_true",
        help="Allow an expired cache, including as an explicit refresh fallback",
    )

    daily = reference_commands.add_parser(
        "daily",
        help="Fetch configured A-share daily bars",
    )
    _add_reference_common_arguments(daily)
    daily.add_argument(
        "--date",
        required=True,
        type=_iso_date,
        help="Trading date in YYYY-MM-DD format",
    )

    market_data = subparsers.add_parser(
        "market-data",
        help="Run an isolated market-data shadow snapshot",
    )
    market_data_commands = market_data.add_subparsers(
        dest="market_data_command",
        required=True,
    )
    snapshot = market_data_commands.add_parser(
        "snapshot",
        help="Validate a Mock or OpenD quote batch without generating a report",
    )
    snapshot.add_argument(
        "--provider",
        metavar="PROVIDER",
        help="Quote provider; defaults to MARKET_DATA_PROVIDER (mock by default)",
    )
    snapshot.add_argument(
        "--config",
        type=Path,
        help="Override WATCHLIST_CONFIG_PATH for this command",
    )
    snapshot.add_argument(
        "--phase",
        choices=(
            MarketPhase.A_SHARE_CALL_AUCTION.value,
            MarketPhase.A_SHARE_OPEN_PRICE.value,
            MarketPhase.A_SHARE_MIDDAY.value,
            MarketPhase.A_SHARE_CLOSE.value,
        ),
        default=MarketPhase.A_SHARE_MIDDAY.value,
        help="A-share phase attached to the snapshot",
    )
    snapshot.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the watchlist and show the call plan without loading a provider",
    )
    replay = market_data_commands.add_parser(
        "replay",
        help="Replay a validated market-data snapshot without network access",
    )
    replay.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Shadow snapshot JSON file to replay",
    )
    replay.add_argument(
        "--config",
        type=Path,
        help="Override WATCHLIST_CONFIG_PATH for this command",
    )
    replay.add_argument(
        "--write-report",
        action="store_true",
        help="Atomically write a full replay report under data/market-data/replays",
    )

    report = subparsers.add_parser(
        "report",
        help="Generate an offline report from a validated replay snapshot",
    )
    report_commands = report.add_subparsers(
        dest="report_command",
        required=True,
    )
    shadow = report_commands.add_parser(
        "shadow",
        help="Generate a replay-only shadow report with the Mock LLM",
    )
    shadow.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Shadow snapshot JSON file to report on",
    )
    shadow.add_argument(
        "--config",
        type=Path,
        help="Override WATCHLIST_CONFIG_PATH for this command",
    )

    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args() if argv is None else parse_args(argv)
    if args.command == "run-once":
        service = build_report_service()
        phase = MarketPhase(args.phase)
        status = asyncio.run(service.run(phase))
        print(json.dumps({"status": status.value, "phase": phase.value}))
        return 0

    if args.command == "watchlist":
        config_path = args.config or get_settings().watchlist_config_path
        return _run_watchlist_command(config_path)

    if args.command == "reference":
        return asyncio.run(_run_reference_command(args))

    if args.command == "market-data":
        if args.market_data_command == "replay":
            return asyncio.run(run_market_data_replay_command(args))
        return asyncio.run(run_market_data_shadow_command(args))

    if args.command == "report":
        return asyncio.run(run_shadow_report_command(args))

    raise RuntimeError(f"Unsupported command: {args.command}")


def _add_reference_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        help="Override WATCHLIST_CONFIG_PATH for this command",
    )
    parser.add_argument(
        "--security-type",
        choices=("all", "stock", "index"),
        default="all",
        help="Select all configured securities, stocks only, or indices only",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the remote-call plan without loading credentials or calling Tushare",
    )


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from error


async def _run_reference_command(args: argparse.Namespace) -> int:
    settings = None if args.dry_run else get_settings()
    config_path = (
        args.config
        or (
            Path(os.environ.get("WATCHLIST_CONFIG_PATH", "config/watchlist.yaml"))
            if settings is None
            else settings.watchlist_config_path
        )
    )
    try:
        watchlist = WatchlistLoader().load(config_path)
    except WatchlistConfigurationError as error:
        _print_reference_summary(
            status="failed",
            requested_count=0,
            supported_count=0,
            returned_count=0,
            missing_count=0,
            unsupported_count=0,
            invalid_count=0,
            provider_error_counts={"configuration": len(error.issues)},
            output_path=None,
        )
        return 2

    selected = tuple(
        security
        for security in watchlist.securities
        if security.enabled
        and (
            args.security_type == "all"
            or security.security_type.value == args.security_type
        )
    )
    if not selected:
        _print_reference_summary(
            status="failed",
            requested_count=0,
            supported_count=0,
            returned_count=0,
            missing_count=0,
            unsupported_count=0,
            invalid_count=0,
            provider_error_counts={"quality": 1},
            output_path=None,
        )
        return 2

    symbols = tuple(security.symbol for security in selected)
    security_types = {
        security.symbol: _reference_security_type(security.security_type)
        for security in selected
    }
    stock_count = sum(
        security_type is SecurityCategory.STOCK
        for security_type in security_types.values()
    )
    etf_count = sum(
        security_type is SecurityCategory.ETF
        for security_type in security_types.values()
    )
    index_count = sum(
        security_type is SecurityCategory.INDEX
        for security_type in security_types.values()
    )
    supported_count = stock_count + index_count
    if args.dry_run:
        _print_reference_dry_run(
            command=args.reference_command,
            requested_count=len(symbols),
            stock_count=stock_count,
            etf_count=etf_count,
            index_count=index_count,
        )
        return 0

    if settings is None:
        raise RuntimeError("settings must be loaded for a real reference-data request")
    if args.reference_command == "security-master":
        return await _run_security_master_command(
            args=args,
            settings=settings,
            symbols=symbols,
            security_types=security_types,
            etf_count=etf_count,
        )

    try:
        _, daily_bar_provider = (
            build_tushare_reference_providers(settings, security_types)
        )
    except MarketDataProviderError as error:
        _print_reference_summary(
            status="failed",
            requested_count=len(symbols),
            supported_count=supported_count,
            returned_count=0,
            missing_count=0,
            unsupported_count=etf_count,
            invalid_count=0,
            provider_error_counts={_provider_exception_category(error): 1},
            output_path=None,
        )
        return 2

    if args.reference_command == "daily":
        daily_bar_batch = await daily_bar_provider.get_daily_bars(
            symbols,
            args.date,
            args.date,
        )
        batch: SecurityMasterBatch | DailyBarBatch = daily_bar_batch
        output_path = REFERENCE_OUTPUT_DIR / f"daily-{args.date.isoformat()}.json"
        returned_count = len({bar.symbol for bar in daily_bar_batch.bars})
    else:
        raise RuntimeError(
            f"Unsupported reference command: {args.reference_command}"
        )

    _write_reference_output(output_path, batch)
    _print_reference_summary(
        status=batch.completeness.value,
        requested_count=len(batch.requested_symbols),
        supported_count=supported_count,
        returned_count=returned_count,
        missing_count=len(batch.missing_symbols),
        unsupported_count=len(batch.unsupported_symbols),
        invalid_count=len(batch.invalid_symbols),
        provider_error_counts=dict(
            sorted(Counter(error.category.value for error in batch.provider_errors).items())
        ),
        output_path=output_path,
    )
    return 2 if batch.completeness.value == "failed" else 0


async def _run_security_master_command(
    *,
    args: argparse.Namespace,
    settings: Settings,
    symbols: tuple[str, ...],
    security_types: dict[str, SecurityCategory],
    etf_count: int,
) -> int:
    cache_path = REFERENCE_OUTPUT_DIR / "security-master.json"
    cache = SecurityMasterCache(cache_path)
    now = _utc_now()
    max_age = timedelta(days=settings.security_master_max_age_days)

    cached_entry: SecurityMasterCacheEntry | None = None
    cache_error: SecurityMasterCacheError | None = None
    try:
        cached_entry = cache.load(
            expected_symbols=symbols,
            now=now,
            max_age=max_age,
        )
    except SecurityMasterCacheError as error:
        cache_error = error

    if not args.refresh:
        if cached_entry is None:
            _print_security_master_cache_summary(
                status=(
                    "cache_miss"
                    if cache_error is not None and cache_error.code == "cache_miss"
                    else "failed"
                ),
                cache_status=cache_error.code if cache_error else "invalid_cache",
                cache_path=cache_path,
                fetched_at=None,
                age_seconds=None,
                requested_count=len(symbols),
                returned_count=0,
                missing_count=0,
                unsupported_count=etf_count,
                provider_error_counts={},
                network_calls=0,
            )
            return 2
        if cached_entry.is_stale and not args.allow_stale:
            _print_security_master_cache_summary(
                status="cache_stale",
                cache_status="stale",
                cache_path=cache_path,
                fetched_at=cached_entry.document.fetched_at,
                age_seconds=cached_entry.age_seconds,
                requested_count=len(cached_entry.batch.requested_symbols),
                returned_count=len(cached_entry.batch.records),
                missing_count=len(cached_entry.batch.missing_symbols),
                unsupported_count=len(cached_entry.batch.unsupported_symbols),
                provider_error_counts=_provider_error_counts(cached_entry.batch),
                network_calls=0,
            )
            return 2
        _print_security_master_cache_summary(
            status=(
                "stale"
                if cached_entry.is_stale
                else cached_entry.batch.completeness.value
            ),
            cache_status="stale" if cached_entry.is_stale else "fresh",
            cache_path=cache_path,
            fetched_at=cached_entry.document.fetched_at,
            age_seconds=cached_entry.age_seconds,
            requested_count=len(cached_entry.batch.requested_symbols),
            returned_count=len(cached_entry.batch.records),
            missing_count=len(cached_entry.batch.missing_symbols),
            unsupported_count=len(cached_entry.batch.unsupported_symbols),
            provider_error_counts=_provider_error_counts(cached_entry.batch),
            network_calls=0,
        )
        return 0

    planned_network_calls = sum(
        (
            any(
                security_type is SecurityCategory.STOCK
                for security_type in security_types.values()
            ),
            any(
                security_type is SecurityCategory.INDEX
                for security_type in security_types.values()
            ),
        )
    )
    try:
        security_master_provider, _ = build_tushare_reference_providers(
            settings, security_types
        )
    except MarketDataProviderError as error:
        return _handle_security_master_refresh_failure(
            args=args,
            cache_path=cache_path,
            cached_entry=cached_entry,
            requested_count=len(symbols),
            unsupported_count=etf_count,
            provider_error_counts={_provider_exception_category(error): 1},
            network_calls=0,
        )

    try:
        batch = await security_master_provider.get_security_master(symbols)
    except MarketDataProviderError as error:
        return _handle_security_master_refresh_failure(
            args=args,
            cache_path=cache_path,
            cached_entry=cached_entry,
            requested_count=len(symbols),
            unsupported_count=etf_count,
            provider_error_counts={_provider_exception_category(error): 1},
            network_calls=planned_network_calls,
        )

    provider_error_counts = _provider_error_counts(batch)
    if batch.completeness.value == "failed":
        return _handle_security_master_refresh_failure(
            args=args,
            cache_path=cache_path,
            cached_entry=cached_entry,
            requested_count=len(symbols),
            unsupported_count=len(batch.unsupported_symbols),
            provider_error_counts=provider_error_counts,
            network_calls=planned_network_calls,
        )

    refreshed_at = _utc_now()
    try:
        document = cache.write_atomic(batch, fetched_at=refreshed_at)
    except SecurityMasterCacheError:
        _print_security_master_cache_summary(
            status="failed",
            cache_status="cache_write_failed",
            cache_path=cache_path,
            fetched_at=None,
            age_seconds=None,
            requested_count=len(batch.requested_symbols),
            returned_count=len(batch.records),
            missing_count=len(batch.missing_symbols),
            unsupported_count=len(batch.unsupported_symbols),
            provider_error_counts={"cache_write_failed": 1},
            network_calls=planned_network_calls,
        )
        return 2

    _print_security_master_cache_summary(
        status=batch.completeness.value,
        cache_status="refreshed",
        cache_path=cache_path,
        fetched_at=document.fetched_at,
        age_seconds=0,
        requested_count=len(batch.requested_symbols),
        returned_count=len(batch.records),
        missing_count=len(batch.missing_symbols),
        unsupported_count=len(batch.unsupported_symbols),
        provider_error_counts=provider_error_counts,
        network_calls=planned_network_calls,
    )
    return 0


def _handle_security_master_refresh_failure(
    *,
    args: argparse.Namespace,
    cache_path: Path,
    cached_entry: SecurityMasterCacheEntry | None,
    requested_count: int,
    unsupported_count: int,
    provider_error_counts: dict[str, int],
    network_calls: int,
) -> int:
    if args.allow_stale and cached_entry is not None:
        _print_security_master_cache_summary(
            status="stale",
            cache_status="refresh_failed_using_stale",
            cache_path=cache_path,
            fetched_at=cached_entry.document.fetched_at,
            age_seconds=cached_entry.age_seconds,
            requested_count=len(cached_entry.batch.requested_symbols),
            returned_count=len(cached_entry.batch.records),
            missing_count=len(cached_entry.batch.missing_symbols),
            unsupported_count=len(cached_entry.batch.unsupported_symbols),
            provider_error_counts=provider_error_counts,
            network_calls=network_calls,
        )
        return 0

    _print_security_master_cache_summary(
        status="failed",
        cache_status="refresh_failed",
        cache_path=cache_path,
        fetched_at=None,
        age_seconds=None,
        requested_count=requested_count,
        returned_count=0,
        missing_count=0,
        unsupported_count=unsupported_count,
        provider_error_counts=provider_error_counts,
        network_calls=network_calls,
    )
    return 2


def _provider_error_counts(batch: SecurityMasterBatch) -> dict[str, int]:
    return dict(
        sorted(Counter(error.category.value for error in batch.provider_errors).items())
    )


def _print_security_master_cache_summary(
    *,
    status: str,
    cache_status: str,
    cache_path: Path,
    fetched_at: datetime | None,
    age_seconds: int | None,
    requested_count: int,
    returned_count: int,
    missing_count: int,
    unsupported_count: int,
    provider_error_counts: dict[str, int],
    network_calls: int,
) -> None:
    print(
        json.dumps(
            {
                "status": status,
                "cache_status": cache_status,
                "cache_path": cache_path.as_posix(),
                "fetched_at": fetched_at.isoformat() if fetched_at else None,
                "age_seconds": age_seconds,
                "requested_count": requested_count,
                "returned_count": returned_count,
                "missing_count": missing_count,
                "unsupported_count": unsupported_count,
                "provider_error_counts": dict(sorted(provider_error_counts.items())),
                "network_calls": network_calls,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _reference_security_type(security_type: SecurityType) -> SecurityCategory:
    return {
        SecurityType.STOCK: SecurityCategory.STOCK,
        SecurityType.ETF: SecurityCategory.ETF,
        SecurityType.INDEX: SecurityCategory.INDEX,
    }[security_type]


def _provider_exception_category(error: MarketDataProviderError) -> str:
    if isinstance(error, MarketDataAuthorizationError):
        return "authorization"
    if isinstance(error, MarketDataRateLimitError):
        return "rate_limited"
    if isinstance(error, MarketDataTimeoutError):
        return "timeout"
    if isinstance(error, MarketDataQualityError):
        return "quality"
    return "provider_error"


def _write_reference_output(
    output_path: Path,
    batch: SecurityMasterBatch | DailyBarBatch,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            batch.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _print_reference_summary(
    *,
    status: str,
    requested_count: int,
    supported_count: int,
    returned_count: int,
    missing_count: int,
    unsupported_count: int,
    invalid_count: int,
    provider_error_counts: dict[str, int],
    output_path: Path | None,
) -> None:
    print(
        json.dumps(
            {
                "status": status,
                "requested_count": requested_count,
                "supported_count": supported_count,
                "returned_count": returned_count,
                "missing_count": missing_count,
                "unsupported_count": unsupported_count,
                "invalid_count": invalid_count,
                "provider_error_counts": dict(sorted(provider_error_counts.items())),
                "output_path": output_path.as_posix() if output_path else None,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _print_reference_dry_run(
    *,
    command: str,
    requested_count: int,
    stock_count: int,
    etf_count: int,
    index_count: int,
) -> None:
    api_names = (
        ("stock_basic", "index_basic")
        if command == "security-master"
        else ("daily", "index_daily")
    )
    planned_calls = {
        api_names[0]: int(stock_count > 0),
        api_names[1]: int(index_count > 0),
    }
    print(
        json.dumps(
            {
                "status": "dry_run",
                "requested_count": requested_count,
                "supported_count": stock_count + index_count,
                "stock_count": stock_count,
                "etf_count": etf_count,
                "index_count": index_count,
                "unsupported_count": etf_count,
                "planned_calls": planned_calls,
                "total_planned_calls": sum(planned_calls.values()),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _run_watchlist_command(config_path: Path) -> int:
    try:
        watchlist = WatchlistLoader().load(config_path)
    except WatchlistConfigurationError as exc:
        print(
            json.dumps(
                _invalid_watchlist_payload(exc),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2

    print(
        json.dumps(
            _valid_watchlist_payload(config_path, watchlist),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _valid_watchlist_payload(
    config_path: Path,
    watchlist: WatchlistConfig,
) -> dict[str, object]:
    securities = watchlist.securities
    return {
        "status": "valid",
        "config_path": config_path.as_posix(),
        "total_count": len(securities),
        "enabled_count": sum(security.enabled for security in securities),
        "stock_count": sum(
            security.security_type is SecurityType.STOCK for security in securities
        ),
        "etf_count": sum(
            security.security_type is SecurityType.ETF for security in securities
        ),
        "holding_count": sum(
            SecurityRole.HOLDING in security.roles for security in securities
        ),
        "critical_count": sum(
            security.priority is WatchPriority.CRITICAL for security in securities
        ),
        "exchanges": {
            exchange.value: sum(
                security.exchange is exchange for security in securities
            )
            for exchange in AShareExchange
        },
        "validation_errors": [],
    }


def _invalid_watchlist_payload(
    error: WatchlistConfigurationError,
) -> dict[str, object]:
    return {
        "status": "invalid",
        "config_path": error.config_path.as_posix(),
        "total_count": None,
        "enabled_count": None,
        "stock_count": None,
        "etf_count": None,
        "holding_count": None,
        "critical_count": None,
        "exchanges": None,
        "validation_errors": [issue.as_dict() for issue in error.issues],
    }


if __name__ == "__main__":
    raise SystemExit(main())
