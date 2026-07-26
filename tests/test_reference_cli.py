from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr

from market_sentinel import cli
from market_sentinel.config import Settings
from market_sentinel.domain.models import TradingMarket
from market_sentinel.domain.security_data import (
    AdjustmentMode,
    Currency,
    DailyBar,
    DailyBarBatch,
    DataCompleteness,
    ListStatus,
    MarketDataErrorCategory,
    PriceUnit,
    ProviderError,
    SecurityCategory,
    SecurityExchange,
    SecurityMasterBatch,
    SecurityMasterRecord,
    TurnoverUnit,
    VolumeUnit,
)
from market_sentinel.market_data.errors import MarketDataAuthorizationError
from market_sentinel.market_data.reference import DailyBarProvider, SecurityMasterProvider

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXED_NOW = datetime(2026, 7, 24, 8, 0, tzinfo=UTC)
TOKEN = "cli-token-that-must-not-appear"


def write_watchlist(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "declared_count": 3,
                "securities": [
                    {
                        "symbol": "600183.SH",
                        "name": "示例股票",
                        "market": "a_share",
                        "exchange": "SH",
                        "security_type": "stock",
                        "enabled": True,
                        "roles": ["watch"],
                        "priority": "normal",
                    },
                    {
                        "symbol": "510300.SH",
                        "name": "示例ETF",
                        "market": "a_share",
                        "exchange": "SH",
                        "security_type": "etf",
                        "enabled": True,
                        "roles": ["watch"],
                        "priority": "normal",
                    },
                    {
                        "symbol": "000001.SH",
                        "name": "示例指数",
                        "market": "a_share",
                        "exchange": "SH",
                        "security_type": "index",
                        "enabled": True,
                        "roles": ["watch"],
                        "priority": "normal",
                    },
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def write_stock_etf_watchlist(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "declared_count": 3,
                "securities": [
                    {
                        "symbol": "600183.SH",
                        "name": "上海示例股票",
                        "market": "a_share",
                        "exchange": "SH",
                        "security_type": "stock",
                        "enabled": True,
                        "roles": ["watch"],
                        "priority": "normal",
                    },
                    {
                        "symbol": "000333.SZ",
                        "name": "深圳示例股票",
                        "market": "a_share",
                        "exchange": "SZ",
                        "security_type": "stock",
                        "enabled": True,
                        "roles": ["watch"],
                        "priority": "normal",
                    },
                    {
                        "symbol": "510300.SH",
                        "name": "示例ETF",
                        "market": "a_share",
                        "exchange": "SH",
                        "security_type": "etf",
                        "enabled": True,
                        "roles": ["watch"],
                        "priority": "normal",
                    },
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


class StubSecurityMasterProvider(SecurityMasterProvider):
    def __init__(self) -> None:
        self.requested: tuple[str, ...] = ()

    async def get_security_master(self, symbols: Sequence[str]) -> SecurityMasterBatch:
        self.requested = tuple(symbols)
        records = tuple(
            SecurityMasterRecord(
                symbol=symbol,
                name=f"fixture-{symbol}",
                market=TradingMarket.A_SHARE,
                exchange=SecurityExchange.XSHG,
                security_type=(
                    SecurityCategory.STOCK
                    if symbol == "600183.SH"
                    else SecurityCategory.INDEX
                ),
                currency=Currency.CNY if symbol == "600183.SH" else None,
                list_status=ListStatus.LISTED if symbol == "600183.SH" else None,
                list_date=None,
                source="tushare_pro",
                provider_symbol=symbol,
                received_at=FIXED_NOW,
            )
            for symbol in symbols
            if symbol != "510300.SH"
        )
        return SecurityMasterBatch(
            requested_symbols=tuple(symbols),
            records=records,
            unsupported_symbols=(
                ("510300.SH",) if "510300.SH" in symbols else ()
            ),
            completeness=(
                DataCompleteness.PARTIAL
                if "510300.SH" in symbols
                else DataCompleteness.COMPLETE
            ),
            source="tushare_pro",
            requested_at=FIXED_NOW,
            completed_at=FIXED_NOW,
        )


class StubDailyBarProvider(DailyBarProvider):
    def __init__(self) -> None:
        self.requested: tuple[str, ...] = ()

    async def get_daily_bars(
        self,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> DailyBarBatch:
        self.requested = tuple(symbols)
        assert start_date == end_date == date(2026, 7, 24)
        bars = tuple(
            DailyBar(
                symbol=symbol,
                trade_date=start_date,
                source="tushare_pro",
                received_at=FIXED_NOW,
                previous_close=Decimal("10.00"),
                open=Decimal("10.10"),
                high=Decimal("10.50"),
                low=Decimal("9.90"),
                close=Decimal("10.20"),
                price_unit=PriceUnit.CNY_PER_SECURITY,
                volume=100,
                turnover=Decimal("1020.00"),
                volume_unit=VolumeUnit.SHARE,
                turnover_unit=TurnoverUnit.CNY,
                adjustment=AdjustmentMode.NONE,
            )
            for symbol in symbols
        )
        return DailyBarBatch(
            requested_symbols=tuple(symbols),
            bars=bars,
            completeness=DataCompleteness.COMPLETE,
            source="tushare_pro",
            requested_at=FIXED_NOW,
            completed_at=FIXED_NOW,
        )


class FailedDailyBarProvider(DailyBarProvider):
    async def get_daily_bars(
        self,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> DailyBarBatch:
        assert start_date == end_date
        return DailyBarBatch(
            requested_symbols=tuple(symbols),
            bars=(),
            unsupported_symbols=(
                ("510300.SH",) if "510300.SH" in symbols else ()
            ),
            provider_errors=(
                ProviderError(
                    category=MarketDataErrorCategory.RATE_LIMIT,
                    code="rate_limited",
                    message="完整但不应出现在终端的限频错误",
                ),
            ),
            completeness=DataCompleteness.FAILED,
            source="tushare",
            requested_at=FIXED_NOW,
            completed_at=FIXED_NOW,
        )


def settings_for(path: Path) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        watchlist_config_path=path,
        tushare_token=SecretStr(TOKEN),
    )  # type: ignore[call-arg]


def test_security_master_cli_outputs_summary_and_writes_normalized_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    watchlist_path = tmp_path / "watchlist.yaml"
    write_watchlist(watchlist_path)
    master = StubSecurityMasterProvider()
    daily = StubDailyBarProvider()
    output_dir = tmp_path / "data/reference/tushare"
    monkeypatch.setattr(cli, "get_settings", lambda: settings_for(watchlist_path))
    monkeypatch.setattr(
        cli,
        "build_tushare_reference_providers",
        lambda settings, security_types: (master, daily),
    )
    monkeypatch.setattr(cli, "REFERENCE_OUTPUT_DIR", output_dir)

    exit_code = cli.main(["reference", "security-master"])

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert exit_code == 0
    assert payload == {
        "status": "partial",
        "requested_count": 3,
        "supported_count": 2,
        "returned_count": 2,
        "missing_count": 0,
        "unsupported_count": 1,
        "invalid_count": 0,
        "provider_error_counts": {},
        "output_path": (output_dir / "security-master.json").as_posix(),
    }
    assert master.requested == ("000001.SH", "510300.SH", "600183.SH")
    assert TOKEN not in output
    assert "fixture-" not in output
    saved = json.loads(
        (output_dir / "security-master.json").read_text(encoding="utf-8")
    )
    assert len(saved["records"]) == 2
    assert saved["unsupported_symbols"] == ["510300.SH"]


@pytest.mark.parametrize(
    ("security_type", "expected_symbol"),
    [
        ("stock", "600183.SH"),
        ("index", "000001.SH"),
    ],
)
def test_daily_cli_can_select_only_stock_or_index(
    security_type: str,
    expected_symbol: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    watchlist_path = tmp_path / "watchlist.yaml"
    write_watchlist(watchlist_path)
    master = StubSecurityMasterProvider()
    daily = StubDailyBarProvider()
    output_dir = tmp_path / "data/reference/tushare"
    monkeypatch.setattr(cli, "get_settings", lambda: settings_for(watchlist_path))
    monkeypatch.setattr(
        cli,
        "build_tushare_reference_providers",
        lambda settings, security_types: (master, daily),
    )
    monkeypatch.setattr(cli, "REFERENCE_OUTPUT_DIR", output_dir)

    exit_code = cli.main(
        [
            "reference",
            "daily",
            "--date",
            "2026-07-24",
            "--security-type",
            security_type,
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "complete"
    assert payload["requested_count"] == 1
    assert payload["supported_count"] == 1
    assert payload["returned_count"] == 1
    assert daily.requested == (expected_symbol,)
    assert payload["output_path"] == (
        output_dir / "daily-2026-07-24.json"
    ).as_posix()


def test_reference_output_directory_is_git_ignored() -> None:
    ignored = subprocess.run(
        [
            "git",
            "check-ignore",
            "--no-index",
            "--quiet",
            "data/reference/tushare/security-master.json",
        ],
        cwd=PROJECT_ROOT,
        check=False,
    )

    assert ignored.returncode == 0


def test_reference_cli_reports_missing_token_without_network_or_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    watchlist_path = tmp_path / "watchlist.yaml"
    write_watchlist(watchlist_path)
    monkeypatch.setattr(cli, "get_settings", lambda: settings_for(watchlist_path))

    def fail_before_client(
        settings: Settings,
        security_types: object,
    ) -> tuple[SecurityMasterProvider, DailyBarProvider]:
        raise MarketDataAuthorizationError("TUSHARE_TOKEN is required")

    monkeypatch.setattr(
        cli,
        "build_tushare_reference_providers",
        fail_before_client,
    )

    exit_code = cli.main(
        ["reference", "daily", "--date", "2026-07-24", "--security-type", "stock"]
    )

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert exit_code == 2
    assert payload["status"] == "failed"
    assert payload["provider_error_counts"] == {"authorization": 1}
    assert "provider_errors" not in payload
    assert TOKEN not in output
    assert payload["output_path"] is None


def test_reference_cli_summarizes_provider_errors_without_printing_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    watchlist_path = tmp_path / "watchlist.yaml"
    write_watchlist(watchlist_path)
    output_dir = tmp_path / "data/reference/tushare"
    monkeypatch.setattr(cli, "get_settings", lambda: settings_for(watchlist_path))
    monkeypatch.setattr(
        cli,
        "build_tushare_reference_providers",
        lambda settings, security_types: (
            StubSecurityMasterProvider(),
            FailedDailyBarProvider(),
        ),
    )
    monkeypatch.setattr(cli, "REFERENCE_OUTPUT_DIR", output_dir)

    exit_code = cli.main(
        ["reference", "daily", "--date", "2026-07-24"]
    )

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert exit_code == 2
    assert payload["status"] == "failed"
    assert payload["provider_error_counts"] == {"rate_limited": 1}
    assert "provider_errors" not in payload
    assert "完整但不应出现在终端的限频错误" not in output
    report = json.loads(
        (output_dir / "daily-2026-07-24.json").read_text(encoding="utf-8")
    )
    assert report["provider_errors"][0]["message"] == (
        "完整但不应出现在终端的限频错误"
    )


@pytest.mark.parametrize(
    ("command_args", "expected_calls"),
    [
        (
            ["reference", "security-master"],
            {"index_basic": 0, "stock_basic": 1},
        ),
        (
            ["reference", "daily", "--date", "2026-07-24"],
            {"daily": 1, "index_daily": 0},
        ),
    ],
)
def test_reference_dry_run_shows_one_batch_call_without_loading_settings(
    command_args: list[str],
    expected_calls: dict[str, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    watchlist_path = tmp_path / "watchlist.yaml"
    write_stock_etf_watchlist(watchlist_path)

    def fail_if_settings_are_loaded() -> Settings:
        raise AssertionError("dry-run must not load Settings or credentials")

    monkeypatch.setattr(cli, "get_settings", fail_if_settings_are_loaded)
    exit_code = cli.main(
        [
            *command_args,
            "--config",
            str(watchlist_path),
            "--dry-run",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload == {
        "status": "dry_run",
        "requested_count": 3,
        "supported_count": 2,
        "stock_count": 2,
        "etf_count": 1,
        "index_count": 0,
        "unsupported_count": 1,
        "planned_calls": expected_calls,
        "total_planned_calls": 1,
    }
    assert TOKEN not in json.dumps(payload)
