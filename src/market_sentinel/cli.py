import argparse
import asyncio
import json

from market_sentinel.bootstrap import build_report_service
from market_sentinel.domain.models import MarketPhase


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="market-sentinel")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_once = subparsers.add_parser("run-once", help="Run one report immediately")
    run_once.add_argument("phase", choices=[item.value for item in MarketPhase])

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "run-once":
        service = build_report_service()
        phase = MarketPhase(args.phase)
        status = asyncio.run(service.run(phase))
        print(json.dumps({"status": status.value, "phase": phase.value}))


if __name__ == "__main__":
    main()
