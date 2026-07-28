from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from market_sentinel.domain.models import ActionState, MarketPhase, TradingMarket
from market_sentinel.domain.quotes import (
    MarketQuote,
    QuoteBatch,
    QuoteFreshness,
    QuoteMarketState,
    TradingStatus,
)
from market_sentinel.domain.security_data import (
    Currency,
    DataCompleteness,
    SecurityCategory,
    SecurityExchange,
)
from market_sentinel.llm.shadow_mock import (
    MockShadowNarrator,
    ShadowNarrative,
    ShadowNarrativeInput,
    ShadowNarrator,
)
from market_sentinel.market_data.replay import (
    MarketSnapshotReader,
    SnapshotReplayMarketDataProvider,
)
from market_sentinel.market_data.shadow import build_shadow_report
from market_sentinel.reporting.markdown import ShadowReportMarkdownRenderer
from market_sentinel.reporting.shadow import (
    ShadowReportService,
    build_shadow_report_summary,
    calculate_market_statistics,
    run_shadow_report_command,
)

ORIGINAL_REQUESTED_AT = datetime(2026, 7, 27, 6, 59, 59, tzinfo=UTC)
ORIGINAL_COMPLETED_AT = datetime(2026, 7, 27, 7, 0, 1, tzinfo=UTC)
SOURCE_TIME = datetime(2026, 7, 27, 7, 0, 0, tzinfo=UTC)
REPLAYED_AT = datetime(2026, 7, 28, 1, 2, 3, tzinfo=UTC)
PHASE = MarketPhase.A_SHARE_CLOSE
CRITICAL_SYMBOLS = ("510300.SH", "588200.SH", "600183.SH")
BASE_SYMBOLS = CRITICAL_SYMBOLS + ("000333.SZ",)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _security_type(symbol: str) -> SecurityCategory:
    return (
        SecurityCategory.ETF
        if symbol in {"510300.SH", "588200.SH"}
        else SecurityCategory.STOCK
    )


def _quote(
    symbol: str,
    *,
    previous_close: str = "10",
    last: str | None = "10",
    trading_status: TradingStatus = TradingStatus.CLOSED,
    turnover: str = "1000.25",
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
        security_type=_security_type(symbol),
        currency=Currency.CNY,
        source="futu_opend",
        source_time=SOURCE_TIME,
        received_at=ORIGINAL_COMPLETED_AT,
        previous_close=Decimal(previous_close),
        open=Decimal(10),
        high=Decimal(12),
        low=Decimal(8),
        last=Decimal(last) if last is not None else None,
        volume=1000,
        turnover=Decimal(turnover),
        market_phase=PHASE,
        trading_status=trading_status,
    )


def _batch(
    quotes: Sequence[MarketQuote],
    *,
    requested_symbols: Sequence[str] | None = None,
) -> QuoteBatch:
    requested = tuple(sorted(requested_symbols or [quote.symbol for quote in quotes]))
    quote_symbols = {quote.symbol for quote in quotes}
    missing = tuple(sorted(set(requested) - quote_symbols))
    completeness = (
        DataCompleteness.PARTIAL if missing and quotes else DataCompleteness.COMPLETE
    )
    critical_missing = tuple(sorted(set(missing) & set(CRITICAL_SYMBOLS)))
    return QuoteBatch(
        requested_symbols=requested,
        quotes=tuple(quotes),
        missing_symbols=missing,
        critical_missing_symbols=critical_missing,
        returned_count=len(quotes),
        completeness=completeness,
        coverage_ratio=Decimal(len(quotes)) / Decimal(len(requested)),
        source="futu_opend",
        market_phase=PHASE,
        market_state=QuoteMarketState.CLOSED,
        raw_market_states=("STIB_AFTER_HOURS_END",),
        freshness=QuoteFreshness.OUTSIDE_CONTINUOUS_TRADING,
        requested_at=ORIGINAL_REQUESTED_AT,
        completed_at=ORIGINAL_COMPLETED_AT,
    )


def _watch_security(symbol: str) -> dict[str, object]:
    holding = symbol in CRITICAL_SYMBOLS
    return {
        "symbol": symbol,
        "name": f"名称-{symbol}",
        "market": "a_share",
        "exchange": symbol[-2:],
        "security_type": _security_type(symbol).value,
        "enabled": True,
        "roles": ["holding", "watch"] if holding else ["watch"],
        "priority": "critical" if holding else "normal",
    }


def _write_watchlist(path: Path, symbols: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "declared_count": len(symbols),
                "securities": [_watch_security(symbol) for symbol in symbols],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _snapshot_payload(
    quotes: Sequence[MarketQuote],
    *,
    requested_symbols: Sequence[str] | None = None,
) -> dict[str, Any]:
    return build_shadow_report(
        "opend",
        _batch(quotes, requested_symbols=requested_symbols),
    )


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def _loaded_snapshot(
    tmp_path: Path,
    quotes: Sequence[MarketQuote],
    *,
    requested_symbols: Sequence[str] | None = None,
) -> tuple[Path, Any]:
    path = tmp_path / "snapshot.json"
    _write_json(
        path,
        _snapshot_payload(quotes, requested_symbols=requested_symbols),
    )
    return path, MarketSnapshotReader().read(path)


def _loaded_watchlist(tmp_path: Path, symbols: Sequence[str]) -> Any:
    path = tmp_path / "watchlist.yaml"
    _write_watchlist(path, symbols)
    from market_sentinel.watchlist import WatchlistLoader

    return WatchlistLoader().load(path)


def _args(input_path: Path, config_path: Path) -> argparse.Namespace:
    return argparse.Namespace(input=input_path, config=config_path)


def _format_args(
    input_path: Path,
    config_path: Path,
    output_format: str,
) -> argparse.Namespace:
    return argparse.Namespace(
        input=input_path,
        config=config_path,
        format=output_format,
    )


def _renderer_report() -> dict[str, object]:
    return {
        "schema_version": 1,
        "report_id": "shadow-replay-20260728T010203Z",
        "status": "complete",
        "data_mode": "replay",
        "execution_mode": "shadow",
        "input_snapshot_path": "data/market-data/snapshots/example.json",
        "input_provider": "opend",
        "original_requested_at": ORIGINAL_REQUESTED_AT.isoformat(),
        "original_completed_at": ORIGINAL_COMPLETED_AT.isoformat(),
        "replayed_at": REPLAYED_AT.isoformat(),
        "generated_at": REPLAYED_AT.isoformat(),
        "original_market_state": "closed",
        "original_freshness_status": "outside_continuous_trading",
        "source_time_range": {
            "oldest": SOURCE_TIME.isoformat(),
            "newest": SOURCE_TIME.isoformat(),
        },
        "received_at_range": {
            "oldest": ORIGINAL_COMPLETED_AT.isoformat(),
            "newest": ORIGINAL_COMPLETED_AT.isoformat(),
        },
        "completeness": "complete",
        "deterministic_analysis": {
            "requested_count": 4,
            "valid_quote_count": 4,
            "invalid_quote_count": 0,
            "missing_count": 0,
            "critical_missing_symbols": [],
            "advancer_count": 1,
            "decliner_count": 1,
            "unchanged_count": 1,
            "average_change_pct": "1.6667",
            "median_change_pct": "0.0000",
            "maximum_gain_symbol": "510300.SH",
            "maximum_gain_change_pct": "10.0000",
            "maximum_loss_symbol": "588200.SH",
            "maximum_loss_change_pct": "-5.0000",
            "turnover_total": "1000.25",
            "stock_count": 2,
            "etf_count": 2,
            "critical_holdings": [
                {
                    "symbol": "510300.SH",
                    "name": "沪深300ETF华泰柏瑞",
                    "last_price": "11",
                    "previous_close": "10",
                    "change": "1",
                    "change_pct": "10.0000",
                    "trading_status": "closed",
                    "source_time": SOURCE_TIME.isoformat(),
                },
                {
                    "symbol": "588200.SH",
                    "name": "科创芯片ETF嘉实",
                    "last_price": "9.5",
                    "previous_close": "10",
                    "change": "-0.5",
                    "change_pct": "-5.0000",
                    "trading_status": "closed",
                    "source_time": SOURCE_TIME.isoformat(),
                },
                {
                    "symbol": "600183.SH",
                    "name": "生益科技",
                    "last_price": "10",
                    "previous_close": "10",
                    "change": "0",
                    "change_pct": "0.0000",
                    "trading_status": "closed",
                    "source_time": SOURCE_TIME.isoformat(),
                },
            ],
        },
        "risk_result": {
            "action": "no_action",
            "status": "passed",
            "warnings": [],
            "portfolio_exposure_evaluated": False,
            "reason": (
                "position quantities, costs, and market values are not part "
                "of the watchlist"
            ),
        },
        "narrative": {
            "summary": "该离线简报描述录制快照时的行情状态。",
            "observations": ["快照质量门接受 4 条报价。"],
            "limitations": ["未提供新闻或投资建议。"],
        },
        "warnings": [],
        "critical_missing_symbols": [],
        "provider_errors": [],
        "llm_provider": "mock",
        "llm_status": "completed",
        "llm_error": None,
        "network_calls": 0,
    }


def test_deterministic_statistics_cover_prices_counts_and_critical_holdings(
    tmp_path: Path,
) -> None:
    quotes = (
        _quote("510300.SH", last="11", turnover="100"),
        _quote("588200.SH", last="9.5", turnover="200"),
        _quote("600183.SH", last="10", turnover="300"),
        _quote(
            "000333.SZ",
            last=None,
            trading_status=TradingStatus.SUSPENDED,
            turnover="400",
        ),
    )
    batch = _batch(quotes)
    watchlist = _loaded_watchlist(tmp_path, BASE_SYMBOLS)

    analysis = calculate_market_statistics(batch, watchlist)

    assert analysis["advancer_count"] == 1
    assert analysis["decliner_count"] == 1
    assert analysis["unchanged_count"] == 1
    assert analysis["average_change_pct"] == "1.6667"
    assert analysis["median_change_pct"] == "0.0000"
    assert analysis["maximum_gain_symbol"] == "510300.SH"
    assert analysis["maximum_loss_symbol"] == "588200.SH"
    assert analysis["turnover_total"] == "1000"
    assert analysis["stock_count"] == 2
    assert analysis["etf_count"] == 2
    holdings = analysis["critical_holdings"]
    assert isinstance(holdings, list)
    assert [holding["symbol"] for holding in holdings] == list(CRITICAL_SYMBOLS)
    assert holdings[0]["change_pct"] == "10.0000"


def test_zero_previous_close_and_suspended_quote_are_defensively_uncalculable(
    tmp_path: Path,
) -> None:
    zero_previous_close = _quote("600183.SH").model_copy(
        update={"previous_close": Decimal(0)}
    )
    suspended = _quote(
        "000333.SZ",
        last=None,
        trading_status=TradingStatus.SUSPENDED,
    )
    batch = _batch((zero_previous_close, suspended))
    watchlist = _loaded_watchlist(tmp_path, ("600183.SH", "000333.SZ"))

    analysis = calculate_market_statistics(batch, watchlist)

    assert analysis["unpriced_or_uncalculable_count"] == 2
    assert analysis["average_change_pct"] is None
    assert analysis["median_change_pct"] is None


async def test_full_93_symbol_report_is_offline_atomic_and_not_live(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbols = list(BASE_SYMBOLS)
    for code in range(600001, 600200):
        symbol = f"{code:06d}.SH"
        if symbol not in symbols:
            symbols.append(symbol)
        if len(symbols) == 93:
            break
    quotes = tuple(_quote(symbol, last="10.2") for symbol in symbols)
    snapshot_path = tmp_path / "snapshot.json"
    config_path = tmp_path / "watchlist.yaml"
    output_dir = tmp_path / "data" / "reports" / "shadow"
    _write_json(snapshot_path, _snapshot_payload(quotes))
    _write_watchlist(config_path, tuple(reversed(symbols)))
    network_attempts = 0
    replacements: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def reject_network(*args: object, **kwargs: object) -> object:
        nonlocal network_attempts
        network_attempts += 1
        raise AssertionError("network access attempted")

    def track_replace(source: Path, destination: Path) -> None:
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr("socket.create_connection", reject_network)
    monkeypatch.setattr(
        "market_sentinel.reporting.shadow.os.replace",
        track_replace,
    )

    exit_code = await run_shadow_report_command(
        _args(snapshot_path, config_path),
        output_dir=output_dir,
        now=lambda: REPLAYED_AT,
    )

    raw_summary = capsys.readouterr().out
    summary = json.loads(raw_summary)
    assert exit_code == 0
    assert raw_summary == json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n"
    assert summary["status"] == "complete"
    assert summary["requested_count"] == 93
    assert summary["valid_quote_count"] == 93
    assert summary["network_calls"] == 0
    assert network_attempts == 0
    assert len(replacements) == 1
    assert replacements[0][0].suffix == ".tmp"
    report = json.loads(Path(summary["output_path"]).read_text(encoding="utf-8"))
    assert report["data_mode"] == "replay"
    assert report["execution_mode"] == "shadow"
    assert report["original_market_state"] == "closed"
    assert report["original_freshness_status"] == "outside_continuous_trading"
    assert report["network_calls"] == 0
    assert len(report["facts"]) == 93
    assert report["facts"][0]["previous_close"] == "10"
    assert report["generated_at"].endswith("+00:00")
    assert "live" not in json.dumps(report["narrative"], ensure_ascii=False).lower()


@pytest.mark.parametrize(
    ("missing_symbol", "is_critical"),
    [
        ("000333.SZ", False),
        ("588200.SH", True),
    ],
)
async def test_partial_report_distinguishes_normal_and_critical_missing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    missing_symbol: str,
    is_critical: bool,
) -> None:
    quotes = tuple(_quote(symbol) for symbol in BASE_SYMBOLS if symbol != missing_symbol)
    snapshot_path = tmp_path / "snapshot.json"
    config_path = tmp_path / "watchlist.yaml"
    _write_json(
        snapshot_path,
        _snapshot_payload(quotes, requested_symbols=BASE_SYMBOLS),
    )
    _write_watchlist(config_path, BASE_SYMBOLS)

    exit_code = await run_shadow_report_command(
        _args(snapshot_path, config_path),
        output_dir=tmp_path / "reports",
        now=lambda: REPLAYED_AT,
    )

    summary = json.loads(capsys.readouterr().out)
    report = json.loads(Path(summary["output_path"]).read_text(encoding="utf-8"))
    assert exit_code == 0
    assert summary["status"] == "partial"
    assert summary["missing_count"] == 1
    assert (missing_symbol in summary["critical_missing_symbols"]) is is_critical
    assert report["risk_result"]["action"] == "no_action"
    if is_critical:
        assert any(
            warning["code"] == "CRITICAL_QUOTE_MISSING"
            for warning in report["risk_result"]["warnings"]
        )


async def test_invalid_and_all_invalid_quotes_follow_quality_gate_semantics(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    config_path = tmp_path / "watchlist.yaml"
    payload = _snapshot_payload(tuple(_quote(symbol) for symbol in BASE_SYMBOLS))
    payload["quotes"][0]["previous_close"] = "0"
    _write_json(snapshot_path, payload)
    _write_watchlist(config_path, BASE_SYMBOLS)

    partial_exit = await run_shadow_report_command(
        _args(snapshot_path, config_path),
        output_dir=tmp_path / "partial",
        now=lambda: REPLAYED_AT,
    )
    partial_summary = json.loads(capsys.readouterr().out)
    partial_report = json.loads(
        Path(partial_summary["output_path"]).read_text(encoding="utf-8")
    )

    for quote in payload["quotes"]:
        quote["previous_close"] = "0"
    _write_json(snapshot_path, payload)
    failed_exit = await run_shadow_report_command(
        _args(snapshot_path, config_path),
        output_dir=tmp_path / "failed",
        now=lambda: REPLAYED_AT,
    )
    failed_summary = json.loads(capsys.readouterr().out)
    failed_report = json.loads(
        Path(failed_summary["output_path"]).read_text(encoding="utf-8")
    )

    assert partial_exit == 0
    assert partial_summary["status"] == "partial"
    assert partial_report["deterministic_analysis"]["invalid_quote_count"] == 1
    assert failed_exit == 2
    assert failed_summary["status"] == "failed"
    assert failed_report["facts"] == []
    assert failed_report["llm_status"] == "skipped_data_failed"


@pytest.mark.parametrize("bad_previous_close", ["0", None])
async def test_zero_or_missing_previous_close_is_rejected_without_division(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    bad_previous_close: str | None,
) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    config_path = tmp_path / "watchlist.yaml"
    payload = _snapshot_payload(tuple(_quote(symbol) for symbol in BASE_SYMBOLS))
    if bad_previous_close is None:
        payload["quotes"][0].pop("previous_close")
    else:
        payload["quotes"][0]["previous_close"] = bad_previous_close
    _write_json(snapshot_path, payload)
    _write_watchlist(config_path, BASE_SYMBOLS)

    exit_code = await run_shadow_report_command(
        _args(snapshot_path, config_path),
        output_dir=tmp_path / "reports",
        now=lambda: REPLAYED_AT,
    )

    summary = json.loads(capsys.readouterr().out)
    report = json.loads(Path(summary["output_path"]).read_text(encoding="utf-8"))
    assert exit_code == 0
    assert summary["status"] == "partial"
    assert report["deterministic_analysis"]["invalid_quote_count"] == 1
    assert report["deterministic_analysis"]["valid_quote_count"] == 3


@pytest.mark.parametrize("issue", ["duplicate", "unexpected"])
async def test_duplicate_and_unexpected_quotes_remain_explicit_in_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    issue: str,
) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    config_path = tmp_path / "watchlist.yaml"
    payload = _snapshot_payload(tuple(_quote(symbol) for symbol in BASE_SYMBOLS))
    if issue == "duplicate":
        payload["quotes"].append(dict(payload["quotes"][0]))
        affected_symbol = payload["quotes"][0]["symbol"]
    else:
        affected_symbol = "600001.SH"
        payload["quotes"].append(
            _quote(affected_symbol).model_dump(mode="json")
        )
    _write_json(snapshot_path, payload)
    _write_watchlist(config_path, BASE_SYMBOLS)

    exit_code = await run_shadow_report_command(
        _args(snapshot_path, config_path),
        output_dir=tmp_path / "reports",
        now=lambda: REPLAYED_AT,
    )

    summary = json.loads(capsys.readouterr().out)
    report = json.loads(Path(summary["output_path"]).read_text(encoding="utf-8"))
    assert exit_code == 0
    assert summary["status"] == "partial"
    field = "duplicate_symbols" if issue == "duplicate" else "unexpected_symbols"
    assert report[field] == [affected_symbol]


class FailingNarrator(ShadowNarrator):
    async def generate(self, inputs: ShadowNarrativeInput) -> ShadowNarrative:
        raise RuntimeError("token=SecretValue123456789 account=998877")


async def test_mock_llm_failure_preserves_deterministic_report_and_redacts_secrets(
    tmp_path: Path,
) -> None:
    snapshot_path, snapshot = _loaded_snapshot(
        tmp_path,
        tuple(_quote(symbol, last="10.2") for symbol in BASE_SYMBOLS),
    )
    watchlist = _loaded_watchlist(tmp_path, BASE_SYMBOLS)
    provider = SnapshotReplayMarketDataProvider(snapshot, now=lambda: REPLAYED_AT)
    service = ShadowReportService(
        provider=provider,
        narrator=FailingNarrator(),
        output_dir=tmp_path / "reports",
        now=lambda: REPLAYED_AT,
    )

    run = await service.run(snapshot, watchlist)

    serialized = json.dumps(run.report, ensure_ascii=False)
    assert snapshot_path.exists()
    assert run.status == "partial"
    assert run.report["llm_status"] == "failed"
    assert run.report["narrative"] is None
    assert run.report["facts"]
    assert run.report["deterministic_analysis"]
    assert "SecretValue123456789" not in serialized
    assert "998877" not in serialized


async def test_mock_narrative_is_stable_trimmed_and_cannot_change_risk_action() -> None:
    inputs = ShadowNarrativeInput(
        completeness=DataCompleteness.COMPLETE,
        requested_count=93,
        valid_quote_count=93,
        advancer_count=50,
        decliner_count=40,
        unchanged_count=3,
        critical_missing_symbols=(),
        warning_codes=(),
        risk_action=ActionState.NO_ACTION,
    )
    narrator = MockShadowNarrator()

    first = await narrator.generate(inputs)
    second = await narrator.generate(inputs)

    assert first == second
    assert set(first.model_dump()) == {"summary", "observations", "limitations"}
    rendered = json.dumps(first.model_dump(mode="json"), ensure_ascii=False)
    assert "新闻" in rendered
    assert "投资建议" in rendered
    assert all(term not in rendered for term in ("买入", "卖出", "正在上涨"))


def test_facts_are_provenanced_and_contain_no_portfolio_calculations(
    tmp_path: Path,
) -> None:
    _, snapshot = _loaded_snapshot(
        tmp_path,
        tuple(_quote(symbol) for symbol in BASE_SYMBOLS),
    )
    watchlist = _loaded_watchlist(tmp_path, BASE_SYMBOLS)
    analysis = calculate_market_statistics(snapshot.batch, watchlist)

    serialized = json.dumps(analysis, ensure_ascii=False)
    assert all(
        forbidden not in serialized
        for forbidden in (
            "cost",
            "profit",
            "market_value",
            "position_ratio",
            "buy",
            "sell",
        )
    )
    holdings = analysis["critical_holdings"]
    assert isinstance(holdings, list)
    assert set(holdings[0]) == {
        "symbol",
        "name",
        "last_price",
        "previous_close",
        "change",
        "change_pct",
        "trading_status",
        "source_time",
    }


async def test_source_times_are_preserved_and_replay_time_is_separate(
    tmp_path: Path,
) -> None:
    _, snapshot = _loaded_snapshot(
        tmp_path,
        tuple(_quote(symbol) for symbol in BASE_SYMBOLS),
    )
    watchlist = _loaded_watchlist(tmp_path, BASE_SYMBOLS)

    run = await ShadowReportService(
        provider=SnapshotReplayMarketDataProvider(snapshot, now=lambda: REPLAYED_AT),
        output_dir=tmp_path / "reports",
        now=lambda: REPLAYED_AT,
    ).run(snapshot, watchlist)

    facts = run.report["facts"]
    assert isinstance(facts, list)
    assert all(fact["source"] == "futu_opend" for fact in facts)
    assert all(fact["source_time"] == SOURCE_TIME.isoformat() for fact in facts)
    assert all(
        fact["received_at"] == ORIGINAL_COMPLETED_AT.isoformat() for fact in facts
    )
    assert run.report["replayed_at"] == REPLAYED_AT.isoformat()
    assert run.report["generated_at"] == REPLAYED_AT.isoformat()


async def test_output_order_is_stable_for_reversed_watchlist(
    tmp_path: Path,
) -> None:
    quotes = tuple(_quote(symbol, last="10.2") for symbol in BASE_SYMBOLS)
    _, snapshot = _loaded_snapshot(tmp_path, quotes)
    first_watchlist = _loaded_watchlist(tmp_path / "first", BASE_SYMBOLS)
    second_watchlist = _loaded_watchlist(
        tmp_path / "second",
        tuple(reversed(BASE_SYMBOLS)),
    )
    first = await ShadowReportService(
        provider=SnapshotReplayMarketDataProvider(snapshot, now=lambda: REPLAYED_AT),
        output_dir=tmp_path / "first-report",
        now=lambda: REPLAYED_AT,
    ).run(snapshot, first_watchlist)
    second = await ShadowReportService(
        provider=SnapshotReplayMarketDataProvider(snapshot, now=lambda: REPLAYED_AT),
        output_dir=tmp_path / "second-report",
        now=lambda: REPLAYED_AT,
    ).run(snapshot, second_watchlist)

    first_report = dict(first.report)
    second_report = dict(second.report)
    assert first_report == second_report
    facts = first_report["facts"]
    assert isinstance(facts, list)
    assert [fact["symbol"] for fact in facts] == sorted(BASE_SYMBOLS)


@pytest.mark.parametrize(
    ("filename", "contents", "category"),
    [
        ("missing.json", None, "file_not_found"),
        ("broken.json", '{"cookie":"SecretValue123456789"', "invalid_json"),
        ("new.json", '{"schema_version":999}', "unsupported_schema_version"),
    ],
)
async def test_bad_snapshot_is_concise_offline_and_nonzero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    filename: str,
    contents: str | None,
    category: str,
) -> None:
    snapshot_path = tmp_path / filename
    config_path = tmp_path / "watchlist.yaml"
    _write_watchlist(config_path, BASE_SYMBOLS)
    if contents is not None:
        snapshot_path.write_text(contents, encoding="utf-8")

    exit_code = await run_shadow_report_command(
        _args(snapshot_path, config_path),
        output_dir=tmp_path / "reports",
    )

    output = capsys.readouterr().out
    summary = json.loads(output)
    assert exit_code == 2
    assert summary["provider_error_counts"] == {category: 1}
    assert summary["network_calls"] == 0
    assert "SecretValue123456789" not in output


async def test_missing_watchlist_and_report_write_failure_are_nonzero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    config_path = tmp_path / "watchlist.yaml"
    _write_json(
        snapshot_path,
        _snapshot_payload(tuple(_quote(symbol) for symbol in BASE_SYMBOLS)),
    )

    missing_config_exit = await run_shadow_report_command(
        _args(snapshot_path, config_path),
        output_dir=tmp_path / "reports",
    )
    missing_config_summary = json.loads(capsys.readouterr().out)

    _write_watchlist(config_path, BASE_SYMBOLS)

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("password=SecretValue123456789")

    monkeypatch.setattr(
        "market_sentinel.reporting.shadow.os.replace",
        fail_replace,
    )
    output_exit = await run_shadow_report_command(
        _args(snapshot_path, config_path),
        output_dir=tmp_path / "reports",
        now=lambda: REPLAYED_AT,
    )
    output_summary_raw = capsys.readouterr().out
    output_summary = json.loads(output_summary_raw)

    assert missing_config_exit == 2
    assert missing_config_summary["provider_error_counts"] == {"configuration": 1}
    assert output_exit == 2
    assert output_summary["provider_error_counts"] == {"output_write_failed": 1}
    assert "SecretValue123456789" not in output_summary_raw


def test_cli_parser_and_main_expose_shadow_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from market_sentinel import cli

    args = cli.parse_args(
        ["report", "shadow", "--input", "snapshot.json", "--config", "watch.yaml"]
    )
    assert args.report_command == "shadow"
    assert args.input == Path("snapshot.json")
    assert args.config == Path("watch.yaml")

    async def fake_run(parsed: argparse.Namespace) -> int:
        assert parsed.report_command == "shadow"
        return 2

    monkeypatch.setattr(cli, "run_shadow_report_command", fake_run)
    assert cli.main(["report", "shadow", "--input", "broken.json"]) == 2


def test_shadow_summary_is_stable_and_never_expands_quote_rows(
    tmp_path: Path,
) -> None:
    _, snapshot = _loaded_snapshot(
        tmp_path,
        tuple(_quote(symbol) for symbol in BASE_SYMBOLS),
    )
    report: dict[str, object] = {
        "completeness": "complete",
        "deterministic_analysis": {
            "requested_count": 4,
            "valid_quote_count": 4,
            "missing_count": 0,
            "critical_missing_symbols": [],
            "advancer_count": 0,
            "decliner_count": 0,
            "unchanged_count": 4,
        },
        "risk_result": {"action": "no_action"},
        "warnings": [],
        "llm_status": "completed",
    }
    from market_sentinel.reporting.shadow import ShadowReportRun

    summary = build_shadow_report_summary(
        ShadowReportRun("complete", report, tmp_path / "report.json"),
        snapshot,
    )

    assert summary["network_calls"] == 0
    assert "facts" not in summary
    assert "narrative" not in summary
    assert list(summary) == [
        "status",
        "execution_mode",
        "data_mode",
        "input_path",
        "input_provider",
        "completeness",
        "requested_count",
        "valid_quote_count",
        "missing_count",
        "critical_missing_symbols",
        "advancer_count",
        "decliner_count",
        "unchanged_count",
        "warning_count",
        "risk_action",
        "llm_provider",
        "llm_status",
        "network_calls",
        "output_path",
        "json_output_path",
        "markdown_output_path",
    ]


def test_markdown_renderer_contains_complete_auditable_sections() -> None:
    report = _renderer_report()

    markdown = ShadowReportMarkdownRenderer().render(
        report,
        json_output_path=Path(
            "data/reports/shadow/20260728T010203Z-replay-report.json"
        ),
    )

    assert markdown.startswith("# MarketSentinel 市场简报\n")
    assert "## 报告信息" in markdown
    assert "完整（complete）" in markdown
    assert "> 本报告基于历史快照回放" in markdown
    assert "## 数据质量" in markdown
    assert "- 请求证券数：4" in markdown
    assert "- 有效报价数：4" in markdown
    assert "## 市场概览" in markdown
    assert "- 平均涨跌幅：1.6667%" in markdown
    assert "- 中位数涨跌幅：0.0000%" in markdown
    assert "510300.SH（10.0000%）" in markdown
    assert "588200.SH（-5.0000%）" in markdown
    assert "- 总成交额：1000.25" in markdown
    assert "source_time 范围" in markdown
    assert "received_at 范围" in markdown
    assert "JSON 权威报告路径" in markdown


def test_markdown_has_exact_three_critical_holdings_without_position_values() -> None:
    markdown = ShadowReportMarkdownRenderer().render(
        _renderer_report(),
        json_output_path=Path("report.json"),
    )

    table_rows = [
        line
        for line in markdown.splitlines()
        if line.startswith("| ") and not line.startswith("| ---")
    ]
    assert len(table_rows) == 4
    assert sum("510300.SH" in line for line in table_rows) == 1
    assert sum("588200.SH" in line for line in table_rows) == 1
    assert sum("600183.SH" in line for line in table_rows) == 1
    assert "portfolio_exposure_evaluated：false" in markdown
    assert "本报告不能判断组合风险高低" in markdown
    assert "建议继续持有" not in markdown
    assert "买入" not in markdown
    assert "卖出" not in markdown


@pytest.mark.parametrize(
    ("completeness", "expected_label"),
    [
        ("complete", "完整（complete）"),
        ("partial", "部分可用（partial）"),
        ("failed", "失败（failed）"),
    ],
)
def test_markdown_uses_explicit_completeness_labels(
    completeness: str,
    expected_label: str,
) -> None:
    report = _renderer_report()
    report["completeness"] = completeness
    report["status"] = completeness

    markdown = ShadowReportMarkdownRenderer().render(
        report,
        json_output_path=None,
    )

    assert expected_label in markdown
    assert "本次未生成" in markdown


def test_markdown_renders_empty_narrative_llm_failure_and_warnings() -> None:
    report = _renderer_report()
    report["narrative"] = None
    report["llm_status"] = "failed"
    report["critical_missing_symbols"] = ["588200.SH"]
    report["warnings"] = [
        {
            "code": "MISSING_QUOTE",
            "severity": "critical",
            "message": "critical quote unavailable",
            "symbol": "588200.SH",
        }
    ]
    report["provider_errors"] = [
        {
            "category": "timeout",
            "code": "timeout",
            "message": "provider timeout",
            "symbol": None,
        }
    ]

    markdown = ShadowReportMarkdownRenderer().render(
        report,
        json_output_path=Path("report.json"),
    )

    assert "Mock LLM 未生成叙述（状态：failed）" in markdown
    assert "MISSING_QUOTE" in markdown
    assert "588200.SH" in markdown
    assert "provider timeout" in markdown
    assert "- LLM 状态：failed" in markdown


def test_markdown_escapes_tables_headings_code_and_sensitive_text() -> None:
    report = _renderer_report()
    analysis = report["deterministic_analysis"]
    assert isinstance(analysis, dict)
    holdings = analysis["critical_holdings"]
    assert isinstance(holdings, list)
    holdings[0]["name"] = "名称|伪造\n# 标题 `代码`"
    report["warnings"] = [
        {
            "code": "unsafe",
            "severity": "high",
            "message": (
                "token=SecretValue123456789 "
                "email=user@example.com password=anotherSecret123456"
            ),
            "symbol": "510300.SH",
        }
    ]

    markdown = ShadowReportMarkdownRenderer().render(
        report,
        json_output_path=Path("report.json"),
    )

    assert "名称\\|伪造 \\# 标题 \\`代码\\`" in markdown
    assert "\n# 标题" not in markdown
    assert "SecretValue123456789" not in markdown
    assert "anotherSecret123456" not in markdown
    assert "user@example.com" not in markdown
    assert "[redacted-secret]" in markdown
    assert "[redacted-email]" in markdown


@pytest.mark.parametrize(
    ("output_format", "expect_json", "expect_markdown"),
    [
        ("json", True, False),
        ("markdown", False, True),
        ("both", True, True),
    ],
)
async def test_cli_output_formats_and_json_default_compatibility(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    output_format: str,
    expect_json: bool,
    expect_markdown: bool,
) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    config_path = tmp_path / "watchlist.yaml"
    _write_json(
        snapshot_path,
        _snapshot_payload(tuple(_quote(symbol) for symbol in BASE_SYMBOLS)),
    )
    _write_watchlist(config_path, BASE_SYMBOLS)

    exit_code = await run_shadow_report_command(
        _format_args(snapshot_path, config_path, output_format),
        output_dir=tmp_path / "data" / "reports" / "shadow",
        now=lambda: REPLAYED_AT,
    )

    summary = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert (summary["json_output_path"] is not None) is expect_json
    assert (summary["markdown_output_path"] is not None) is expect_markdown
    if expect_json:
        json_path = Path(summary["json_output_path"])
        assert json_path.exists()
        json_report = json.loads(json_path.read_text(encoding="utf-8"))
        assert json_report["schema_version"] == 1
        assert summary["output_path"] == summary["json_output_path"]
    if expect_markdown:
        markdown_path = Path(summary["markdown_output_path"])
        assert markdown_path.exists()
        assert markdown_path.read_text(encoding="utf-8").startswith(
            "# MarketSentinel 市场简报\n"
        )
        if not expect_json:
            assert summary["output_path"] == summary["markdown_output_path"]


async def test_both_outputs_share_report_id_and_are_atomically_replaced(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    config_path = tmp_path / "watchlist.yaml"
    _write_json(
        snapshot_path,
        _snapshot_payload(tuple(_quote(symbol) for symbol in BASE_SYMBOLS)),
    )
    _write_watchlist(config_path, BASE_SYMBOLS)
    replacements: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def track_replace(source: Path, destination: Path) -> None:
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(
        "market_sentinel.reporting.shadow.os.replace",
        track_replace,
    )

    exit_code = await run_shadow_report_command(
        _format_args(snapshot_path, config_path, "both"),
        output_dir=tmp_path / "reports",
        now=lambda: REPLAYED_AT,
    )

    summary = json.loads(capsys.readouterr().out)
    json_path = Path(summary["json_output_path"])
    markdown_path = Path(summary["markdown_output_path"])
    report = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")
    assert exit_code == 0
    assert json_path.stem == markdown_path.stem
    assert report["report_id"] in markdown
    assert len(replacements) == 2
    assert all(source.suffix == ".tmp" for source, _ in replacements)
    assert {destination.suffix for _, destination in replacements} == {".json", ".md"}


async def test_markdown_uses_existing_report_without_recomputing_or_network(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    config_path = tmp_path / "watchlist.yaml"
    _write_json(
        snapshot_path,
        _snapshot_payload(tuple(_quote(symbol) for symbol in BASE_SYMBOLS)),
    )
    _write_watchlist(config_path, BASE_SYMBOLS)
    network_attempts = 0

    def reject_network(*args: object, **kwargs: object) -> object:
        nonlocal network_attempts
        network_attempts += 1
        raise AssertionError("network attempted")

    monkeypatch.setattr("socket.create_connection", reject_network)

    exit_code = await run_shadow_report_command(
        _format_args(snapshot_path, config_path, "markdown"),
        output_dir=tmp_path / "reports",
        now=lambda: REPLAYED_AT,
    )

    summary = json.loads(capsys.readouterr().out)
    markdown = Path(summary["markdown_output_path"]).read_text(encoding="utf-8")
    assert exit_code == 0
    assert summary["network_calls"] == 0
    assert network_attempts == 0
    assert "## 市场概览" in markdown
    assert "## Facts" not in markdown
    assert "510300.SH" in markdown


def test_shadow_markdown_output_directory_is_git_ignored() -> None:
    ignored = subprocess.run(
        [
            "git",
            "check-ignore",
            "--no-index",
            "--quiet",
            "data/reports/shadow/example-replay-report.md",
        ],
        cwd=PROJECT_ROOT,
        check=False,
    )

    assert ignored.returncode == 0


def test_cli_format_defaults_to_json() -> None:
    from market_sentinel.cli import parse_args

    default_args = parse_args(
        ["report", "shadow", "--input", "snapshot.json"]
    )
    markdown_args = parse_args(
        [
            "report",
            "shadow",
            "--input",
            "snapshot.json",
            "--format",
            "markdown",
        ]
    )

    assert default_args.format == "json"
    assert markdown_args.format == "markdown"
