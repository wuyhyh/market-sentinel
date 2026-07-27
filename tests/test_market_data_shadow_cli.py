from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from market_sentinel.config import Settings
from market_sentinel.domain.models import MarketPhase, TradingMarket
from market_sentinel.domain.quotes import (
    MarketQuote,
    QualityIssue,
    QualitySeverity,
    QuoteBatch,
    QuoteFreshness,
    QuoteMarketState,
    TradingStatus,
)
from market_sentinel.domain.security_data import (
    Currency,
    DataCompleteness,
    MarketDataErrorCategory,
    ProviderError,
    SecurityCategory,
    SecurityExchange,
)
from market_sentinel.market_data.base import QuoteMarketDataProvider
from market_sentinel.market_data.shadow import (
    build_shadow_provider,
    run_market_data_shadow_command,
)

FIXED_NOW = datetime(2026, 7, 27, 2, 0, 1, tzinfo=UTC)
PHASE = MarketPhase.A_SHARE_MIDDAY
CRITICAL_SYMBOLS = ("510300.SH", "588200.SH", "600183.SH")


def _security(
    symbol: str,
    *,
    security_type: str,
    holding: bool = False,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "name": f"证券{symbol}",
        "market": "a_share",
        "exchange": symbol[-2:],
        "security_type": security_type,
        "enabled": True,
        "roles": ["holding", "watch"] if holding else ["watch"],
        "priority": "critical" if holding else "normal",
    }


def _write_watchlist(
    path: Path,
    securities: Sequence[Mapping[str, object]],
) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "declared_count": len(securities),
                "securities": [dict(security) for security in securities],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _four_securities() -> list[dict[str, object]]:
    return [
        _security("588200.SH", security_type="etf", holding=True),
        _security("510300.SH", security_type="etf", holding=True),
        _security("600183.SH", security_type="stock", holding=True),
        _security("000333.SZ", security_type="stock"),
    ]


def _ninety_three_securities() -> list[dict[str, object]]:
    securities = _four_securities()
    for code in range(600001, 600090):
        symbol = f"{code:06d}.SH"
        if symbol != "600183.SH":
            securities.append(_security(symbol, security_type="stock"))
        if len(securities) == 93:
            break
    assert len(securities) == 93
    return securities


def _quote(
    symbol: str,
    *,
    source: str = "opend",
    source_time: datetime = datetime(2026, 7, 27, 2, 0, 0, tzinfo=UTC),
    received_at: datetime = FIXED_NOW,
) -> MarketQuote:
    return MarketQuote(
        symbol=symbol,
        provider_symbol=f"{symbol[-2:]}.{symbol[:6]}",
        exchange=(
            SecurityExchange.XSHG
            if symbol.endswith(".SH")
            else SecurityExchange.XSHE
        ),
        market=TradingMarket.A_SHARE,
        security_type=(
            SecurityCategory.ETF
            if symbol in {"510300.SH", "588200.SH"}
            else SecurityCategory.STOCK
        ),
        currency=Currency.CNY,
        source=source,
        source_time=source_time,
        received_at=received_at,
        previous_close=Decimal("10.00"),
        open=Decimal("10.10"),
        high=Decimal("10.50"),
        low=Decimal("9.90"),
        last=Decimal("10.20"),
        volume=1000,
        turnover=Decimal("10200.50"),
        market_phase=PHASE,
        trading_status=TradingStatus.TRADING,
    )


def _batch(
    requested_symbols: Sequence[str],
    *,
    source: str = "opend",
    missing_symbols: Sequence[str] = (),
    invalid_symbols: Sequence[str] = (),
    duplicate_symbols: Sequence[str] = (),
    unexpected_symbols: Sequence[str] = (),
    provider_errors: Sequence[ProviderError] = (),
    market_state: QuoteMarketState = QuoteMarketState.CONTINUOUS_TRADING,
    raw_market_states: Sequence[str] = (),
    freshness: QuoteFreshness = QuoteFreshness.NOT_VERIFIED_CONTINUOUS_TRADING,
    failed: bool = False,
) -> QuoteBatch:
    requested = tuple(sorted(requested_symbols))
    unavailable = set(missing_symbols) | set(invalid_symbols)
    quotes = (
        ()
        if failed
        else tuple(
            _quote(symbol, source=source)
            for symbol in requested
            if symbol not in unavailable
        )
    )
    issues = bool(
        missing_symbols
        or invalid_symbols
        or duplicate_symbols
        or unexpected_symbols
        or provider_errors
    )
    completeness = (
        DataCompleteness.FAILED
        if failed
        else DataCompleteness.PARTIAL
        if issues
        else DataCompleteness.COMPLETE
    )
    critical_missing = tuple(
        symbol
        for symbol in CRITICAL_SYMBOLS
        if symbol in set(missing_symbols) | set(invalid_symbols)
    )
    quality_issues = (
        (
            QualityIssue(
                code="critical_missing",
                severity=QualitySeverity.CRITICAL,
                message="critical holding is unavailable",
                symbol=critical_missing[0],
            ),
        )
        if critical_missing
        else ()
    )
    return QuoteBatch(
        requested_symbols=requested,
        quotes=quotes,
        missing_symbols=tuple(missing_symbols),
        invalid_symbols=tuple(invalid_symbols),
        duplicate_symbols=tuple(duplicate_symbols),
        unexpected_symbols=tuple(unexpected_symbols),
        critical_missing_symbols=critical_missing,
        provider_errors=tuple(provider_errors),
        quality_issues=quality_issues,
        returned_count=0 if failed else len(quotes) + len(duplicate_symbols),
        snapshot_calls=1,
        market_state_calls=1,
        network_calls=2,
        completeness=completeness,
        coverage_ratio=Decimal(len(quotes)) / Decimal(len(requested)),
        source=source,
        market_phase=PHASE,
        market_state=market_state,
        raw_market_states=tuple(raw_market_states),
        freshness=freshness,
        requested_at=datetime(2026, 7, 27, 1, 59, 59, tzinfo=UTC),
        completed_at=FIXED_NOW,
    )


def _batch_with_symbol_issue(
    symbols: Sequence[str],
    issue_name: str,
    issue_value: tuple[str, ...],
) -> QuoteBatch:
    if issue_name == "duplicate_symbols":
        return _batch(symbols, duplicate_symbols=issue_value)
    if issue_name == "unexpected_symbols":
        return _batch(symbols, unexpected_symbols=issue_value)
    if issue_name == "invalid_symbols":
        return _batch(symbols, invalid_symbols=issue_value)
    raise AssertionError(f"unsupported test issue: {issue_name}")


class FakeProvider(QuoteMarketDataProvider):
    def __init__(
        self,
        factory: Callable[[Sequence[str]], QuoteBatch] | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.factory = factory
        self.error = error
        self.calls: list[tuple[tuple[str, ...], MarketPhase]] = []

    async def get_quotes(
        self,
        symbols: Sequence[str],
        phase: MarketPhase,
    ) -> QuoteBatch:
        self.calls.append((tuple(symbols), phase))
        if self.error is not None:
            raise self.error
        assert self.factory is not None
        return self.factory(symbols)


def _args(
    config: Path,
    *,
    provider: str = "opend",
    dry_run: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        provider=provider,
        config=config,
        phase=PHASE.value,
        dry_run=dry_run,
    )


def _settings() -> Settings:
    return Settings(_env_file=None, market_data_provider="mock")  # type: ignore[call-arg]


async def _run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    provider: QuoteMarketDataProvider,
    *,
    securities: Sequence[Mapping[str, object]] | None = None,
    provider_name: str = "opend",
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "watchlist.yaml"
    _write_watchlist(config, securities or _four_securities())
    exit_code = await run_market_data_shadow_command(
        _args(config, provider=provider_name),
        settings_loader=_settings,
        provider_builder=lambda name, settings, types: provider,
        output_dir=tmp_path / "data" / "market-data" / "snapshots",
        now=lambda: FIXED_NOW,
    )
    summary = json.loads(capsys.readouterr().out)
    report = json.loads(Path(summary["output_path"]).read_text(encoding="utf-8"))
    return exit_code, summary, report


async def test_mock_provider_complete_snapshot_is_offline(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "watchlist.yaml"
    _write_watchlist(config, _four_securities())

    exit_code = await run_market_data_shadow_command(
        _args(config, provider="mock"),
        settings_loader=_settings,
        provider_builder=build_shadow_provider,
        output_dir=tmp_path / "data" / "market-data" / "snapshots",
        now=lambda: FIXED_NOW,
    )

    summary = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert summary["status"] == "complete"
    assert summary["provider"] == "mock"
    assert summary["requested_count"] == summary["returned_count"] == 4
    assert summary["valid_quote_count"] == 4
    assert summary["snapshot_calls"] == 0


async def test_fake_opend_complete_snapshot_calls_provider_once_for_93_symbols(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider = FakeProvider(lambda symbols: _batch(symbols))

    exit_code, summary, report = await _run(
        tmp_path,
        capsys,
        provider,
        securities=_ninety_three_securities(),
    )

    assert exit_code == 0
    assert len(provider.calls) == 1
    assert len(provider.calls[0][0]) == 93
    assert summary["requested_count"] == summary["returned_count"] == 93
    assert report["requested_symbols"] == sorted(report["requested_symbols"])
    assert [quote["symbol"] for quote in report["quotes"]] == sorted(
        quote["symbol"] for quote in report["quotes"]
    )


@pytest.mark.parametrize(
    ("missing_symbol", "is_critical"),
    [
        ("000333.SZ", False),
        ("588200.SH", True),
    ],
)
async def test_missing_symbol_is_partial_and_critical_holdings_are_visible(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    missing_symbol: str,
    is_critical: bool,
) -> None:
    provider = FakeProvider(
        lambda symbols: _batch(symbols, missing_symbols=(missing_symbol,))
    )

    exit_code, summary, _ = await _run(tmp_path, capsys, provider)

    assert exit_code == 0
    assert summary["status"] == "partial"
    assert summary["missing_count"] == 1
    assert (missing_symbol in summary["critical_missing_symbols"]) is is_critical


@pytest.mark.parametrize(
    ("issue_name", "issue_value", "summary_field"),
    [
        ("duplicate_symbols", ("600183.SH",), "duplicate_count"),
        ("unexpected_symbols", ("600001.SH",), "unexpected_count"),
        ("invalid_symbols", ("000333.SZ",), "invalid_quote_count"),
    ],
)
async def test_batch_quality_issues_are_preserved_in_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    issue_name: str,
    issue_value: tuple[str, ...],
    summary_field: str,
) -> None:
    provider = FakeProvider(
        lambda symbols: _batch_with_symbol_issue(symbols, issue_name, issue_value)
    )

    exit_code, summary, report = await _run(tmp_path, capsys, provider)

    assert exit_code == 0
    assert summary["status"] == "partial"
    assert summary[summary_field] == 1
    assert report[issue_name] == sorted(issue_value)


async def test_provider_failure_is_reported_once_and_returns_nonzero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = ProviderError(
        category=MarketDataErrorCategory.TIMEOUT,
        code="timeout",
        message="snapshot timed out",
    )
    provider = FakeProvider(lambda symbols: _batch(symbols, provider_errors=(error,), failed=True))

    exit_code, summary, report = await _run(tmp_path, capsys, provider)

    assert exit_code == 2
    assert summary["status"] == "failed"
    assert summary["provider_error_counts"] == {"timeout": 1}
    assert len(report["provider_errors"]) == 1


async def test_uncaught_provider_exception_becomes_sanitized_failed_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider = FakeProvider(
        error=RuntimeError(
            "password=SecretValue123456789 user@example.com account=13800138000"
        )
    )

    exit_code, summary, report = await _run(tmp_path, capsys, provider)
    serialized = json.dumps(report, ensure_ascii=False)

    assert exit_code == 2
    assert summary["provider_error_counts"] == {"provider_error": 1}
    assert "SecretValue123456789" not in serialized
    assert "user@example.com" not in serialized
    assert "13800138000" not in serialized


@pytest.mark.parametrize(
    ("market_state", "freshness"),
    [
        (
            QuoteMarketState.CLOSED,
            QuoteFreshness.OUTSIDE_CONTINUOUS_TRADING,
        ),
        (
            QuoteMarketState.CONTINUOUS_TRADING,
            QuoteFreshness.NOT_VERIFIED_CONTINUOUS_TRADING,
        ),
    ],
)
async def test_completeness_and_freshness_are_independent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    market_state: QuoteMarketState,
    freshness: QuoteFreshness,
) -> None:
    provider = FakeProvider(
        lambda symbols: _batch(
            symbols,
            market_state=market_state,
            freshness=freshness,
        )
    )

    exit_code, summary, _ = await _run(tmp_path, capsys, provider)

    assert exit_code == 0
    assert summary["completeness"] == "complete"
    assert summary["freshness_status"] == freshness.value


async def test_raw_market_state_is_kept_only_in_full_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider = FakeProvider(
        lambda symbols: _batch(
            symbols,
            market_state=QuoteMarketState.CLOSED,
            raw_market_states=(
                "STIB_AFTER_HOURS_END",
                "AFTER_HOURS_END",
                "AFTER_HOURS_END",
            ),
            freshness=QuoteFreshness.OUTSIDE_CONTINUOUS_TRADING,
        )
    )

    exit_code, summary, report = await _run(tmp_path, capsys, provider)

    assert exit_code == 0
    assert summary["market_state"] == "closed"
    assert summary["freshness_status"] == "outside_continuous_trading"
    assert "raw_market_state" not in summary
    assert report["raw_market_state"] == {
        "AFTER_HOURS_END": 2,
        "STIB_AFTER_HOURS_END": 1,
    }


async def test_dry_run_validates_watchlist_without_settings_or_provider(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "watchlist.yaml"
    _write_watchlist(config, _four_securities())
    settings_calls = 0
    provider_calls = 0

    def fail_settings() -> Settings:
        nonlocal settings_calls
        settings_calls += 1
        raise AssertionError("settings must not load in dry-run")

    def fail_provider(
        name: str,
        settings: Settings,
        types: Mapping[str, SecurityCategory],
    ) -> QuoteMarketDataProvider:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("provider must not be built in dry-run")

    exit_code = await run_market_data_shadow_command(
        _args(config, dry_run=True),
        settings_loader=fail_settings,
        provider_builder=fail_provider,
        output_dir=tmp_path / "snapshots",
    )

    summary = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert settings_calls == provider_calls == 0
    assert summary == {
        "critical_holding_count": 3,
        "etf_count": 2,
        "index_count": 0,
        "network_calls": 0,
        "output_directory": (tmp_path / "snapshots").as_posix(),
        "planned_market_state_calls": 1,
        "planned_snapshot_calls": 1,
        "provider": "opend",
        "requested_count": 4,
        "status": "dry_run",
        "stock_count": 2,
    }


async def test_invalid_watchlist_returns_configuration_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "missing.yaml"

    exit_code = await run_market_data_shadow_command(
        _args(config, dry_run=True),
    )

    summary = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert summary["provider_error_counts"] == {"configuration": 1}
    assert summary["output_path"] is None


async def test_report_uses_atomic_replace_and_serializes_domain_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacements: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def track_replace(source: Path, destination: Path) -> None:
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(
        "market_sentinel.market_data.shadow.os.replace",
        track_replace,
    )
    provider = FakeProvider(lambda symbols: _batch(symbols))

    _, _, report = await _run(tmp_path, capsys, provider)

    assert len(replacements) == 1
    assert replacements[0][0].suffix == ".tmp"
    assert replacements[0][1].suffix == ".json"
    first_quote = report["quotes"][0]
    assert first_quote["last"] == "10.20"
    assert first_quote["turnover"] == "10200.50"
    assert first_quote["source_time"].endswith("Z")
    assert report["requested_at"].endswith("+00:00")
    assert not list(replacements[0][1].parent.glob("*.tmp"))


async def test_watchlist_order_produces_stable_domain_order(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    securities = _four_securities()
    provider_one = FakeProvider(lambda symbols: _batch(symbols))
    _, summary_one, report_one = await _run(
        tmp_path / "one",
        capsys,
        provider_one,
        securities=securities,
    )
    provider_two = FakeProvider(lambda symbols: _batch(symbols))
    _, summary_two, report_two = await _run(
        tmp_path / "two",
        capsys,
        provider_two,
        securities=tuple(reversed(securities)),
    )

    summary_one.pop("output_path")
    summary_two.pop("output_path")
    assert summary_one == summary_two
    assert report_one["requested_symbols"] == report_two["requested_symbols"]
    assert report_one["quotes"] == report_two["quotes"]


async def test_mock_provider_does_not_require_opend_optional_dependency(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_futu(module_name: str) -> object:
        raise AssertionError(f"unexpected optional import: {module_name}")

    monkeypatch.setattr(
        "market_sentinel.market_data.opend.importlib.import_module",
        reject_futu,
    )
    config = tmp_path / "watchlist.yaml"
    _write_watchlist(config, _four_securities())

    exit_code = await run_market_data_shadow_command(
        _args(config, provider="mock"),
        settings_loader=_settings,
        provider_builder=build_shadow_provider,
        output_dir=tmp_path / "snapshots",
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["provider"] == "mock"


async def test_missing_opend_optional_dependency_is_a_clear_nonzero_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from market_sentinel.market_data import opend

    config = tmp_path / "watchlist.yaml"
    _write_watchlist(config, _four_securities())

    def missing_futu(module_name: str) -> object:
        raise ImportError(f"No module named {module_name}")

    monkeypatch.setattr(
        opend,
        "check_opend_endpoint",
        lambda host, port, timeout: None,
    )
    sdk_importlib: Any = vars(opend)["importlib"]
    monkeypatch.setattr(sdk_importlib, "import_module", missing_futu)
    exit_code = await run_market_data_shadow_command(
        _args(config),
        settings_loader=_settings,
        provider_builder=build_shadow_provider,
        output_dir=tmp_path / "snapshots",
    )

    summary = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert summary["provider_error_counts"] == {"provider_error": 1}
    assert summary["action_required"] == "install market-sentinel[opend]"
    assert summary["snapshot_calls"] == 0
    assert summary["output_path"].endswith("-opend.json")


async def test_output_failure_returns_nonzero_without_printing_domain_details(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeProvider(lambda symbols: _batch(symbols))

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("password=SecretValue123456789")

    monkeypatch.setattr(
        "market_sentinel.market_data.shadow.os.replace",
        fail_replace,
    )
    config = tmp_path / "watchlist.yaml"
    _write_watchlist(config, _four_securities())

    exit_code = await run_market_data_shadow_command(
        _args(config),
        settings_loader=_settings,
        provider_builder=lambda name, settings, types: provider,
        output_dir=tmp_path / "snapshots",
    )

    output = capsys.readouterr().out
    summary = json.loads(output)
    assert exit_code == 2
    assert summary["status"] == "failed"
    assert summary["provider_error_counts"] == {"output_write_failed": 1}
    assert "SecretValue123456789" not in output


def test_cli_parser_defaults_to_safe_provider_selection() -> None:
    from market_sentinel.cli import parse_args

    args = parse_args(["market-data", "snapshot", "--dry-run"])

    assert args.provider is None
    assert args.dry_run is True
    assert args.phase == PHASE.value


async def test_unknown_provider_returns_stable_configuration_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "watchlist.yaml"
    _write_watchlist(config, _four_securities())

    exit_code = await run_market_data_shadow_command(
        _args(config, provider="unknown", dry_run=True),
    )

    summary = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert summary["provider"] == "unknown"
    assert summary["provider_error_counts"] == {"configuration": 1}


def test_cli_main_propagates_shadow_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from market_sentinel import cli

    async def failed_shadow(args: argparse.Namespace) -> int:
        assert args.market_data_command == "snapshot"
        return 2

    monkeypatch.setattr(cli, "run_market_data_shadow_command", failed_shadow)

    assert cli.main(["market-data", "snapshot", "--provider", "mock"]) == 2
