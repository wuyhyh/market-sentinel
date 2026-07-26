import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from market_sentinel.bootstrap import build_report_service
from market_sentinel.config import get_settings
from market_sentinel.domain.models import MarketPhase
from market_sentinel.domain.watchlist import (
    AShareExchange,
    SecurityRole,
    SecurityType,
    WatchlistConfig,
    WatchPriority,
)
from market_sentinel.watchlist import WatchlistConfigurationError, WatchlistLoader


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

    raise RuntimeError(f"Unsupported command: {args.command}")


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
