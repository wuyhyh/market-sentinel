from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from market_sentinel.domain.models import MarketPhase, TradingMarket
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
    MarketDataErrorCategory,
    ProviderError,
    SecurityCategory,
    SecurityExchange,
)
from market_sentinel.market_data.replay import (
    MarketSnapshotReader,
    SnapshotReadError,
    SnapshotReplayMarketDataProvider,
    build_replay_report,
    build_replay_summary,
    run_market_data_replay_command,
)
from market_sentinel.market_data.shadow import build_shadow_report

ORIGINAL_REQUESTED_AT = datetime(2026, 7, 27, 6, 59, 59, tzinfo=UTC)
ORIGINAL_COMPLETED_AT = datetime(2026, 7, 27, 7, 0, 1, tzinfo=UTC)
REPLAYED_AT = datetime(2026, 7, 28, 1, 2, 3, tzinfo=UTC)
SOURCE_TIME = datetime(2026, 7, 27, 7, 0, tzinfo=UTC)
PHASE = MarketPhase.A_SHARE_CLOSE
CRITICAL_SYMBOLS = ("510300.SH", "588200.SH", "600183.SH")


def _security_type(symbol: str) -> SecurityCategory:
    return (
        SecurityCategory.ETF
        if symbol in {"510300.SH", "588200.SH"}
        else SecurityCategory.STOCK
    )


def _quote(
    symbol: str,
    *,
    source: str,
    source_time: datetime = SOURCE_TIME,
    received_at: datetime = ORIGINAL_COMPLETED_AT,
) -> MarketQuote:
    return MarketQuote(
        symbol=symbol,
        provider_symbol=(
            f"{symbol[-2:]}.{symbol[:6]}"
            if source == "futu_opend"
            else symbol
        ),
        exchange=(
            SecurityExchange.XSHG
            if symbol.endswith(".SH")
            else SecurityExchange.XSHE
        ),
        market=TradingMarket.A_SHARE,
        security_type=_security_type(symbol),
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
        trading_status=TradingStatus.CLOSED,
    )


def _complete_batch(
    symbols: Sequence[str],
    *,
    source: str,
    market_state: QuoteMarketState = QuoteMarketState.CLOSED,
    freshness: QuoteFreshness = QuoteFreshness.OUTSIDE_CONTINUOUS_TRADING,
) -> QuoteBatch:
    requested = tuple(sorted(symbols))
    quotes = tuple(_quote(symbol, source=source) for symbol in requested)
    return QuoteBatch(
        requested_symbols=requested,
        quotes=quotes,
        returned_count=len(quotes),
        completeness=DataCompleteness.COMPLETE,
        coverage_ratio=Decimal(1),
        source=source,
        market_phase=PHASE,
        market_state=market_state,
        raw_market_states=("STIB_AFTER_HOURS_END",),
        freshness=freshness,
        requested_at=ORIGINAL_REQUESTED_AT,
        completed_at=ORIGINAL_COMPLETED_AT,
    )


def _payload(
    symbols: Sequence[str] = CRITICAL_SYMBOLS + ("000333.SZ",),
    *,
    provider: str = "mock",
) -> dict[str, Any]:
    source = "futu_opend" if provider == "opend" else "mock"
    return build_shadow_report(
        provider,
        _complete_batch(symbols, source=source),
    )


def _write_payload(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def _watch_security(symbol: str) -> dict[str, object]:
    holding = symbol in CRITICAL_SYMBOLS
    return {
        "symbol": symbol,
        "name": f"证券{symbol}",
        "market": "a_share",
        "exchange": symbol[-2:],
        "security_type": _security_type(symbol).value,
        "enabled": True,
        "roles": ["holding", "watch"] if holding else ["watch"],
        "priority": "critical" if holding else "normal",
    }


def _write_watchlist(path: Path, symbols: Sequence[str]) -> None:
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


def _read_payload(
    tmp_path: Path,
    payload: Mapping[str, object] | None = None,
) -> tuple[Path, Any]:
    path = tmp_path / "snapshot.json"
    _write_payload(path, payload or _payload())
    return path, MarketSnapshotReader().read(path)


@pytest.mark.parametrize(
    ("provider", "expected_source"),
    [
        ("mock", "mock"),
        ("opend", "futu_opend"),
    ],
)
def test_reader_restores_valid_mock_and_opend_snapshots(
    tmp_path: Path,
    provider: str,
    expected_source: str,
) -> None:
    path = tmp_path / f"{provider}.json"
    _write_payload(path, _payload(provider=provider))

    snapshot = MarketSnapshotReader().read(path)

    assert snapshot.input_provider == provider
    assert snapshot.batch.source == expected_source
    assert snapshot.batch.completeness is DataCompleteness.COMPLETE
    assert len(snapshot.batch.quotes) == 4


async def test_replay_returns_all_93_symbols_once_and_never_calls_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbols = tuple(f"{600000 + index:06d}.SH" for index in range(1, 94))
    path, snapshot = _read_payload(tmp_path, _payload(symbols))
    network_attempts = 0

    def reject_network(*args: object, **kwargs: object) -> object:
        nonlocal network_attempts
        network_attempts += 1
        raise AssertionError("replay attempted network access")

    monkeypatch.setattr("socket.create_connection", reject_network)
    provider = SnapshotReplayMarketDataProvider(
        snapshot,
        now=lambda: REPLAYED_AT,
    )

    batch = await provider.get_quotes(tuple(reversed(symbols)), PHASE)

    assert path.exists()
    assert len(batch.quotes) == 93
    assert batch.completeness is DataCompleteness.COMPLETE
    assert batch.network_calls == 0
    assert batch.snapshot_calls == 0
    assert batch.market_state_calls == 0
    assert network_attempts == 0


async def test_subset_is_filtered_and_snapshot_extras_are_unexpected(
    tmp_path: Path,
) -> None:
    _, snapshot = _read_payload(tmp_path)
    requested = ("600183.SH", "000333.SZ")
    provider = SnapshotReplayMarketDataProvider(
        snapshot,
        now=lambda: REPLAYED_AT,
    )

    batch = await provider.get_quotes(tuple(reversed(requested)), PHASE)

    assert tuple(quote.symbol for quote in batch.quotes) == tuple(sorted(requested))
    assert batch.unexpected_symbols == ("510300.SH", "588200.SH")
    assert batch.completeness is DataCompleteness.PARTIAL


@pytest.mark.parametrize(
    ("missing_symbol", "critical"),
    [
        ("000333.SZ", False),
        ("588200.SH", True),
    ],
)
async def test_missing_symbols_and_critical_missing_are_recomputed(
    tmp_path: Path,
    missing_symbol: str,
    critical: bool,
) -> None:
    source_symbols = tuple(
        symbol
        for symbol in CRITICAL_SYMBOLS + ("000333.SZ",)
        if symbol != missing_symbol
    )
    _, snapshot = _read_payload(tmp_path, _payload(source_symbols))
    requested = CRITICAL_SYMBOLS + ("000333.SZ",)

    batch = await SnapshotReplayMarketDataProvider(
        snapshot,
        now=lambda: REPLAYED_AT,
    ).get_quotes(requested, PHASE)

    assert batch.missing_symbols == (missing_symbol,)
    assert (missing_symbol in batch.critical_missing_symbols) is critical
    assert batch.completeness is DataCompleteness.PARTIAL


async def test_duplicate_quote_is_reported_and_removed(tmp_path: Path) -> None:
    payload = _payload()
    duplicate = dict(payload["quotes"][0])
    payload["quotes"].append(duplicate)
    path = tmp_path / "duplicate.json"
    _write_payload(path, payload)

    snapshot = MarketSnapshotReader().read(path)

    symbol = duplicate["symbol"]
    assert snapshot.batch.duplicate_symbols == (symbol,)
    assert snapshot.batch.invalid_symbols == (symbol,)
    assert all(quote.symbol != symbol for quote in snapshot.batch.quotes)
    assert snapshot.batch.completeness is DataCompleteness.PARTIAL

    replay = await SnapshotReplayMarketDataProvider(
        snapshot,
        now=lambda: REPLAYED_AT,
    ).get_quotes(snapshot.batch.requested_symbols, PHASE)

    assert replay.duplicate_symbols == (symbol,)
    assert replay.invalid_symbols == (symbol,)
    assert replay.completeness is DataCompleteness.PARTIAL


def test_quote_not_in_original_request_is_unexpected(tmp_path: Path) -> None:
    payload = _payload()
    extra_quote = _quote("600001.SH", source="mock").model_dump(mode="json")
    payload["quotes"].append(extra_quote)
    path = tmp_path / "unexpected.json"
    _write_payload(path, payload)

    snapshot = MarketSnapshotReader().read(path)

    assert snapshot.batch.unexpected_symbols == ("600001.SH",)
    assert all(quote.symbol != "600001.SH" for quote in snapshot.batch.quotes)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("last", "not-a-decimal"),
        ("source_time", "2026-07-27T15:00:00"),
        ("received_at", "invalid-datetime"),
        ("high", "9.00"),
        ("volume", -1),
        ("turnover", "-0.01"),
    ],
)
def test_invalid_quote_is_rejected_by_domain_quality_gate(
    tmp_path: Path,
    field: str,
    bad_value: object,
) -> None:
    payload = _payload()
    symbol = payload["quotes"][0]["symbol"]
    payload["quotes"][0][field] = bad_value
    path = tmp_path / "invalid-quote.json"
    _write_payload(path, payload)

    snapshot = MarketSnapshotReader().read(path)

    assert symbol in snapshot.batch.invalid_symbols
    assert all(quote.symbol != symbol for quote in snapshot.batch.quotes)
    assert snapshot.batch.completeness is DataCompleteness.PARTIAL
    assert any(
        issue.code == "invalid_snapshot_quote"
        for issue in snapshot.batch.quality_issues
    )


def test_all_invalid_quotes_make_replay_source_failed(tmp_path: Path) -> None:
    payload = _payload()
    for quote in payload["quotes"]:
        quote["previous_close"] = "invalid"
    path = tmp_path / "all-invalid.json"
    _write_payload(path, payload)

    snapshot = MarketSnapshotReader().read(path)

    assert snapshot.batch.quotes == ()
    assert snapshot.batch.completeness is DataCompleteness.FAILED
    assert set(snapshot.batch.invalid_symbols) == set(snapshot.batch.requested_symbols)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda payload: payload.update(schema_version=2), "unsupported_schema_version"),
        (lambda payload: payload.pop("quotes"), "quotes_must_be_a_list"),
        (lambda payload: payload.update(completeness="live"), "invalid_completeness"),
        (lambda payload: payload.update(provider="token=secret"), "invalid_provider"),
    ],
)
def test_incompatible_or_untrusted_structure_is_rejected(
    tmp_path: Path,
    mutation: Any,
    expected_code: str,
) -> None:
    payload = _payload()
    mutation(payload)
    path = tmp_path / "invalid-structure.json"
    _write_payload(path, payload)

    with pytest.raises(SnapshotReadError) as error:
        MarketSnapshotReader().read(path)

    assert error.value.code == expected_code


@pytest.mark.parametrize(
    ("name", "contents", "expected_code"),
    [
        ("empty.json", "", "empty_file"),
        ("broken.json", "{not json", "invalid_json"),
        ("array.json", "[]", "root_must_be_an_object"),
    ],
)
def test_empty_or_broken_json_is_rejected(
    tmp_path: Path,
    name: str,
    contents: str,
    expected_code: str,
) -> None:
    path = tmp_path / name
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(SnapshotReadError) as error:
        MarketSnapshotReader().read(path)

    assert error.value.code == expected_code


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "missing.json"

    with pytest.raises(SnapshotReadError) as error:
        MarketSnapshotReader().read(path)

    assert error.value.code == "file_not_found"


def test_file_read_permission_error_is_normalized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "snapshot.json"
    _write_payload(path, _payload())

    def deny_read(self: Path, *, encoding: str) -> str:
        raise PermissionError("account=secret")

    monkeypatch.setattr(Path, "read_text", deny_read)

    with pytest.raises(SnapshotReadError) as error:
        MarketSnapshotReader().read(path)

    assert error.value.code == "file_read_failed"
    assert "secret" not in str(error.value)


def test_partial_and_failed_snapshot_semantics_are_restored(tmp_path: Path) -> None:
    partial_payload = _payload()
    removed = partial_payload["quotes"].pop()
    partial_payload["missing_symbols"] = [removed["symbol"]]
    partial_payload["completeness"] = "partial"
    partial_payload["quality_gate_result"]["status"] = "partial"
    partial_path = tmp_path / "partial.json"
    _write_payload(partial_path, partial_payload)

    failed_batch = QuoteBatch(
        requested_symbols=("600183.SH",),
        quotes=(),
        provider_errors=(
            ProviderError(
                category=MarketDataErrorCategory.TIMEOUT,
                code="timeout",
                message="snapshot timed out",
            ),
        ),
        returned_count=0,
        completeness=DataCompleteness.FAILED,
        coverage_ratio=Decimal(0),
        source="futu_opend",
        market_phase=PHASE,
        market_state=QuoteMarketState.UNKNOWN,
        freshness=QuoteFreshness.UNKNOWN_MARKET_STATE,
        requested_at=ORIGINAL_REQUESTED_AT,
        completed_at=ORIGINAL_COMPLETED_AT,
    )
    failed_payload = build_shadow_report("opend", failed_batch)
    failed_path = tmp_path / "failed.json"
    _write_payload(failed_path, failed_payload)

    partial = MarketSnapshotReader().read(partial_path)
    failed = MarketSnapshotReader().read(failed_path)

    assert partial.original_completeness is DataCompleteness.PARTIAL
    assert partial.batch.completeness is DataCompleteness.PARTIAL
    assert failed.original_completeness is DataCompleteness.FAILED
    assert failed.batch.completeness is DataCompleteness.FAILED


async def test_original_quote_times_are_immutable_and_replayed_at_is_separate(
    tmp_path: Path,
) -> None:
    _, snapshot = _read_payload(tmp_path)
    original_quotes = {
        quote.symbol: (quote.source_time, quote.received_at)
        for quote in snapshot.batch.quotes
    }

    batch = await SnapshotReplayMarketDataProvider(
        snapshot,
        now=lambda: REPLAYED_AT,
    ).get_quotes(snapshot.batch.requested_symbols, PHASE)

    assert batch.freshness is QuoteFreshness.REPLAY
    assert batch.completed_at == REPLAYED_AT
    assert batch.completed_at != snapshot.original_completed_at
    assert {
        quote.symbol: (quote.source_time, quote.received_at)
        for quote in batch.quotes
    } == original_quotes


async def test_replay_report_is_atomic_and_summary_is_stable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbols = CRITICAL_SYMBOLS + ("000333.SZ",)
    snapshot_path = tmp_path / "snapshot.json"
    watchlist_path = tmp_path / "watchlist.yaml"
    output_dir = tmp_path / "data" / "market-data" / "replays"
    _write_payload(snapshot_path, _payload(symbols))
    _write_watchlist(watchlist_path, tuple(reversed(symbols)))
    replacements: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def track_replace(source: Path, destination: Path) -> None:
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(
        "market_sentinel.market_data.shadow.os.replace",
        track_replace,
    )
    args = argparse.Namespace(
        input=snapshot_path,
        config=watchlist_path,
        write_report=True,
    )

    exit_code = await run_market_data_replay_command(
        args,
        output_dir=output_dir,
        now=lambda: REPLAYED_AT,
    )

    raw_output = capsys.readouterr().out
    summary = json.loads(raw_output)
    assert exit_code == 0
    assert raw_output == json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n"
    assert summary["status"] == "complete"
    assert summary["data_mode"] == "replay"
    assert summary["network_calls"] == 0
    assert summary["original_market_state"] == "closed"
    assert summary["original_freshness_status"] == "outside_continuous_trading"
    assert summary["replayed_at"] == REPLAYED_AT.isoformat()
    assert len(replacements) == 1
    assert replacements[0][0].suffix == ".tmp"
    report = json.loads(Path(summary["output_path"]).read_text(encoding="utf-8"))
    assert report["data_mode"] == "replay"
    assert report["network_calls"] == 0
    assert report["replayed_at"] == REPLAYED_AT.isoformat()
    assert report["quotes"][0]["last"] == "10.20"


async def test_corrupt_snapshot_cli_failure_is_concise_and_nonzero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    symbols = CRITICAL_SYMBOLS
    snapshot_path = tmp_path / "broken.json"
    watchlist_path = tmp_path / "watchlist.yaml"
    snapshot_path.write_text(
        '{"password":"SecretValue123456789"',
        encoding="utf-8",
    )
    _write_watchlist(watchlist_path, symbols)
    args = argparse.Namespace(
        input=snapshot_path,
        config=watchlist_path,
        write_report=False,
    )

    exit_code = await run_market_data_replay_command(args)

    output = capsys.readouterr().out
    summary = json.loads(output)
    assert exit_code == 2
    assert summary["status"] == "failed"
    assert summary["provider_error_counts"] == {"invalid_json": 1}
    assert summary["network_calls"] == 0
    assert "SecretValue123456789" not in output


def test_cli_parser_exposes_replay_without_loading_settings() -> None:
    from market_sentinel.cli import parse_args

    args = parse_args(
        [
            "market-data",
            "replay",
            "--input",
            "snapshot.json",
            "--write-report",
        ]
    )

    assert args.market_data_command == "replay"
    assert args.input == Path("snapshot.json")
    assert args.write_report is True


def test_cli_main_propagates_replay_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from market_sentinel import cli

    async def failed_replay(args: argparse.Namespace) -> int:
        assert args.market_data_command == "replay"
        return 2

    monkeypatch.setattr(cli, "run_market_data_replay_command", failed_replay)

    assert cli.main(
        ["market-data", "replay", "--input", "broken.json"]
    ) == 2


async def test_report_builder_never_marks_replay_as_live(tmp_path: Path) -> None:
    _, snapshot = _read_payload(tmp_path)
    provider = SnapshotReplayMarketDataProvider(
        snapshot,
        now=lambda: REPLAYED_AT,
    )
    batch = await provider.get_quotes(snapshot.batch.requested_symbols, PHASE)

    summary = build_replay_summary(snapshot, batch, output_path=None)
    report = build_replay_report(snapshot, batch)

    assert summary["data_mode"] == "replay"
    quality_gate_result = report["quality_gate_result"]
    assert isinstance(quality_gate_result, Mapping)
    assert quality_gate_result["data_freshness"] == "replay"
    assert "live" not in json.dumps(report)
