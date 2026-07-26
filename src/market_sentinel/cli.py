import argparse
import asyncio
import json
import os
from collections import Counter
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from market_sentinel.bootstrap import build_report_service
from market_sentinel.config import get_settings
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
    build_tushare_reference_providers,
)
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
    try:
        security_master_provider, daily_bar_provider = (
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

    if args.reference_command == "security-master":
        security_master_batch = (
            await security_master_provider.get_security_master(symbols)
        )
        batch: SecurityMasterBatch | DailyBarBatch = security_master_batch
        output_path = REFERENCE_OUTPUT_DIR / "security-master.json"
        returned_count = len(security_master_batch.records)
    elif args.reference_command == "daily":
        daily_bar_batch = await daily_bar_provider.get_daily_bars(
            symbols,
            args.date,
            args.date,
        )
        batch = daily_bar_batch
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
