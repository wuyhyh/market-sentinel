from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Never, cast

import pytest
import yaml

from market_sentinel.domain.watchlist import SecurityType
from scripts.spikes.opend_quote.probe import (
    CRITICAL_HOLDING_SYMBOLS,
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    OPEND_START_RECOMMENDATION,
    REQUIRED_SAMPLE_SYMBOLS,
    ErrorCategory,
    FreshnessAssessment,
    OpenDProbeError,
    OpenDQuoteClientAdapter,
    ProbeConfigurationError,
    ProbeSecurity,
    check_opend_endpoint,
    classify_error,
    internal_to_provider_symbol,
    main,
    provider_to_internal_symbol,
    run_probe,
    sanitize_error_message,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
FIXED_RECEIVED_AT = datetime(2026, 7, 27, 2, 0, 1, tzinfo=UTC)
SECRET = "secret-value-that-must-not-appear"


def make_securities() -> tuple[ProbeSecurity, ...]:
    stocks = {
        ProbeSecurity("600183.SH", SecurityType.STOCK),
        ProbeSecurity("000333.SZ", SecurityType.STOCK),
    }
    for offset in range(77):
        stocks.add(
            ProbeSecurity(f"{601000 + offset:06d}.SH", SecurityType.STOCK)
        )
    etfs = {
        ProbeSecurity("588200.SH", SecurityType.ETF),
        ProbeSecurity("510300.SH", SecurityType.ETF),
        ProbeSecurity("159949.SZ", SecurityType.ETF),
    }
    for offset in range(11):
        etfs.add(
            ProbeSecurity(f"{510500 + offset:06d}.SH", SecurityType.ETF)
        )
    securities = tuple(sorted(stocks | etfs, key=lambda item: item.symbol))
    assert len(securities) == 93
    assert sum(
        security.security_type is SecurityType.STOCK for security in securities
    ) == 79
    assert sum(
        security.security_type is SecurityType.ETF for security in securities
    ) == 14
    return securities


def snapshot_row(
    security: ProbeSecurity,
    *,
    update_time: str = "2026-07-27 10:00:00",
    suspended: bool = False,
) -> dict[str, object]:
    return {
        "code": security.provider_symbol,
        "name": f"fixture-{security.symbol}",
        "update_time": update_time,
        "last_price": 10.2,
        "prev_close_price": 10.0,
        "open_price": 10.1,
        "high_price": 10.5,
        "low_price": 9.9,
        "volume": 1000,
        "turnover": 10200.5,
        "suspension": suspended,
        "sec_status": "NORMAL",
    }


def state_rows(
    securities: Sequence[ProbeSecurity],
    state: str,
) -> list[dict[str, object]]:
    return [
        {"code": security.provider_symbol, "market_state": state}
        for security in securities
    ]


class FakeClient:
    sdk_version = "fake-10.9"

    def __init__(
        self,
        securities: Sequence[ProbeSecurity],
        *,
        market_state: str = "CLOSED",
        snapshot_rows: Sequence[Mapping[str, object]] | None = None,
        state_error: Exception | None = None,
        snapshot_error: Exception | None = None,
    ) -> None:
        self._state_rows = state_rows(securities, market_state)
        self._snapshot_rows = (
            [snapshot_row(security) for security in securities]
            if snapshot_rows is None
            else [dict(row) for row in snapshot_rows]
        )
        self._state_error = state_error
        self._snapshot_error = snapshot_error
        self.market_state_calls: list[tuple[str, ...]] = []
        self.snapshot_calls: list[tuple[str, ...]] = []
        self.close_calls = 0

    def get_market_state(
        self, provider_symbols: Sequence[str]
    ) -> tuple[dict[str, object], ...]:
        self.market_state_calls.append(tuple(provider_symbols))
        if self._state_error is not None:
            raise self._state_error
        return tuple(dict(row) for row in self._state_rows)

    def get_market_snapshot(
        self, provider_symbols: Sequence[str]
    ) -> tuple[dict[str, object], ...]:
        self.snapshot_calls.append(tuple(provider_symbols))
        if self._snapshot_error is not None:
            raise self._snapshot_error
        return tuple(dict(row) for row in self._snapshot_rows)

    def close(self) -> None:
        self.close_calls += 1


class FakeTable:
    def __init__(self, rows: Sequence[Mapping[str, object]]) -> None:
        self.rows = [dict(row) for row in rows]
        self.to_dict_calls = 0

    def to_dict(self, *, orient: str) -> list[dict[str, object]]:
        assert orient == "records"
        self.to_dict_calls += 1
        return [dict(row) for row in self.rows]


class FakeOpenQuoteContext:
    def __init__(
        self,
        snapshot_table: FakeTable,
        state_table: FakeTable,
    ) -> None:
        self.snapshot_table = snapshot_table
        self.state_table = state_table
        self.snapshot_calls: list[tuple[str, ...]] = []
        self.state_calls: list[tuple[str, ...]] = []
        self.closed = False

    def get_market_snapshot(
        self, symbols: Sequence[str]
    ) -> tuple[int, FakeTable]:
        self.snapshot_calls.append(tuple(symbols))
        return 0, self.snapshot_table

    def get_market_state(
        self, symbols: Sequence[str]
    ) -> tuple[int, FakeTable]:
        self.state_calls.append(tuple(symbols))
        return 0, self.state_table

    def close(self) -> None:
        self.closed = True


class FakeFutuModule:
    RET_OK = 0
    __version__ = "fake-sdk"

    def __init__(self, context: FakeOpenQuoteContext) -> None:
        self.context = context
        self.context_arguments: tuple[str, int] | None = None

    def OpenQuoteContext(
        self,
        *,
        host: str,
        port: int,
    ) -> FakeOpenQuoteContext:
        self.context_arguments = (host, port)
        return self.context


def fixed_monotonic() -> Callable[[], float]:
    values = iter((10.0, 10.125))
    return lambda: next(values)


def run_fixed_probe(
    client: FakeClient,
    securities: Sequence[ProbeSecurity],
    *,
    expect_live: bool = False,
) -> dict[str, object]:
    return run_probe(
        client,
        securities,
        host="127.0.0.1",
        port=11111,
        expect_live=expect_live,
        now=lambda: FIXED_RECEIVED_AT,
        monotonic=fixed_monotonic(),
    )


def report_records(
    report: Mapping[str, object],
) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], report["records"])


def report_errors(
    report: Mapping[str, object],
) -> list[dict[str, str]]:
    return cast(list[dict[str, str]], report["errors"])


def write_watchlist(path: Path, securities: Sequence[ProbeSecurity]) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "declared_count": len(securities),
                "securities": [
                    {
                        "symbol": security.symbol,
                        "name": f"fixture-{security.symbol}",
                        "market": "a_share",
                        "exchange": security.symbol[-2:],
                        "security_type": security.security_type.value,
                        "enabled": True,
                        "roles": ["watch"],
                        "priority": "normal",
                    }
                    for security in securities
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("internal", "provider"),
    [
        ("600183.SH", "SH.600183"),
        ("000333.SZ", "SZ.000333"),
        ("588200.SH", "SH.588200"),
        ("159949.SZ", "SZ.159949"),
    ],
)
def test_symbol_normalization_is_bidirectional(
    internal: str,
    provider: str,
) -> None:
    assert internal_to_provider_symbol(internal) == provider
    assert provider_to_internal_symbol(provider) == internal


def test_adapter_converts_dataframe_inside_boundary() -> None:
    securities = make_securities()[:2]
    snapshot_table = FakeTable([snapshot_row(security) for security in securities])
    state_table = FakeTable(state_rows(securities, "CLOSED"))
    context = FakeOpenQuoteContext(snapshot_table, state_table)
    module = FakeFutuModule(context)
    adapter = OpenDQuoteClientAdapter(
        "127.0.0.1",
        11111,
        futu_module=module,
    )

    states = adapter.get_market_state(
        [security.provider_symbol for security in securities]
    )
    snapshots = adapter.get_market_snapshot(
        [security.provider_symbol for security in securities]
    )
    adapter.close()

    assert module.context_arguments == ("127.0.0.1", 11111)
    assert isinstance(states, tuple)
    assert isinstance(snapshots, tuple)
    assert all(isinstance(row, dict) for row in (*states, *snapshots))
    assert all(not isinstance(row, FakeTable) for row in (*states, *snapshots))
    assert snapshot_table.to_dict_calls == state_table.to_dict_calls == 1
    assert context.closed is True


def test_all_93_symbols_use_one_market_state_and_one_snapshot_call() -> None:
    securities = make_securities()
    client = FakeClient(securities)

    report = run_fixed_probe(client, securities)

    expected_provider_symbols = tuple(
        security.provider_symbol for security in securities
    )
    assert client.snapshot_calls == [expected_provider_symbols]
    assert client.market_state_calls == [expected_provider_symbols]
    assert report["snapshot_calls"] == 1
    assert report["market_state_calls"] == 1
    assert report["network_calls"] == 2
    assert report["requested_count"] == 93
    assert report["returned_count"] == 93
    assert report["stock_count"] == 79
    assert report["etf_count"] == 14


def test_four_required_samples_and_three_holdings_are_present() -> None:
    securities = make_securities()
    report = run_fixed_probe(FakeClient(securities), securities)

    assert report["required_sample_coverage"] == {
        symbol: True for symbol in REQUIRED_SAMPLE_SYMBOLS
    }
    assert report["critical_holding_coverage"] == {
        symbol: True for symbol in CRITICAL_HOLDING_SYMBOLS
    }


def test_stock_and_etf_rows_convert_to_provider_neutral_records() -> None:
    securities = make_securities()
    report = run_fixed_probe(FakeClient(securities), securities)
    records = {
        record["symbol"]: record
        for record in report_records(report)
    }

    assert records["600183.SH"]["security_type"] == "stock"
    assert records["588200.SH"]["security_type"] == "etf"
    assert records["600183.SH"]["last"] == "10.2"
    assert records["600183.SH"]["previous_close"] == "10.0"
    assert records["600183.SH"]["volume"] == "1000"
    assert records["600183.SH"]["turnover"] == "10200.5"
    assert records["600183.SH"]["volume_unit"] == (
        "unknown_requires_verification"
    )


def test_update_time_and_received_at_are_independent_and_delay_is_computed() -> None:
    securities = make_securities()
    report = run_fixed_probe(FakeClient(securities), securities)
    first = report_records(report)[0]

    assert first["provider_update_time"] == "2026-07-27T10:00:00+08:00"
    assert first["received_at"] == "2026-07-27T02:00:01+00:00"
    assert first["delay_ms"] == 1000
    assert report["elapsed_ms"] == 125.0


def test_closed_market_never_passes_live_freshness() -> None:
    securities = make_securities()
    report = run_fixed_probe(FakeClient(securities, market_state="CLOSED"), securities)

    assert report["status"] == "success"
    assert report["market_state"] == "CLOSED"
    assert report["live_freshness_verified"] is False
    assert report["continuous_updates_verified"] is False
    assert report["freshness_assessment"] == (
        FreshnessAssessment.NOT_VERIFIED_OUTSIDE_CONTINUOUS_TRADING.value
    )
    assert {
        record["freshness_assessment"]
        for record in report_records(report)
    } == {
        FreshnessAssessment.NOT_VERIFIED_OUTSIDE_CONTINUOUS_TRADING.value
    }


@pytest.mark.parametrize("market_state", ["MORNING", "AFTERNOON"])
def test_continuous_market_states_allow_explicit_live_single_snapshot_checks(
    market_state: str,
) -> None:
    securities = make_securities()
    report = run_fixed_probe(
        FakeClient(securities, market_state=market_state),
        securities,
        expect_live=True,
    )

    assert report["status"] == "success"
    assert report["live_freshness_verified"] is True
    assert report["continuous_updates_verified"] is False
    assert report["freshness_assessment"] == (
        FreshnessAssessment.LIVE_CHECKS_PASSED_SINGLE_SNAPSHOT_ONLY.value
    )


def test_future_update_time_fails_expect_live() -> None:
    securities = make_securities()
    rows = [
        snapshot_row(security, update_time="2026-07-27 10:00:02")
        for security in securities
    ]
    report = run_fixed_probe(
        FakeClient(securities, market_state="MORNING", snapshot_rows=rows),
        securities,
        expect_live=True,
    )

    assert report["status"] == "failed"
    assert report["live_freshness_verified"] is False
    assert report["freshness_assessment"] == (
        FreshnessAssessment.LIVE_CHECKS_FAILED.value
    )


def test_missing_duplicate_and_unexpected_symbols_are_explicit() -> None:
    securities = make_securities()
    missing = securities[0]
    duplicate = securities[1]
    rows = [
        snapshot_row(security)
        for security in securities
        if security != missing
    ]
    rows.append(snapshot_row(duplicate))
    unexpected = snapshot_row(
        ProbeSecurity("600999.SH", SecurityType.STOCK)
    )
    rows.append(unexpected)

    report = run_fixed_probe(
        FakeClient(securities, snapshot_rows=rows),
        securities,
    )

    assert report["status"] == "partial"
    assert report["missing_symbols"] == [missing.symbol]
    assert report["duplicate_symbols"] == [duplicate.symbol]
    assert report["unexpected_symbols"] == ["600999.SH"]
    assert all(
        record["symbol"] != "600999.SH"
        for record in report_records(report)
    )


def test_suspension_is_preserved_without_inventing_a_trade() -> None:
    securities = make_securities()
    rows = [
        snapshot_row(
            security,
            suspended=security.symbol == "600183.SH",
        )
        for security in securities
    ]
    report = run_fixed_probe(
        FakeClient(securities, snapshot_rows=rows),
        securities,
    )
    record = next(
        item
        for item in report_records(report)
        if item["symbol"] == "600183.SH"
    )

    assert record["suspended"] is True
    assert record["security_status"] == "NORMAL"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            ConnectionRefusedError("OpenD is not started"),
            ErrorCategory.CONNECTION_REFUSED,
        ),
        (RuntimeError("qot login failed"), ErrorCategory.AUTHENTICATION_FAILED),
        (RuntimeError("no quote right"), ErrorCategory.PERMISSION_DENIED),
        (RuntimeError("frequency limit exceeded"), ErrorCategory.RATE_LIMITED),
        (TimeoutError("request timed out"), ErrorCategory.TIMEOUT),
        (ValueError("bad table"), ErrorCategory.INVALID_RESPONSE),
        ("malformed protocol response", ErrorCategory.PROTOCOL_ERROR),
        (RuntimeError("unknown SDK failure"), ErrorCategory.UNEXPECTED_ERROR),
    ],
)
def test_error_classification(
    error: Exception | str,
    expected: ErrorCategory,
) -> None:
    assert classify_error(error, operation="test").category is expected


def test_snapshot_failure_is_reported_once_without_retries() -> None:
    securities = make_securities()
    client = FakeClient(
        securities,
        snapshot_error=OpenDProbeError(
            ErrorCategory.PERMISSION_DENIED,
            "get_market_snapshot",
            "no quote right",
        ),
    )

    report = run_fixed_probe(client, securities)

    assert report["status"] == "failed"
    assert report["returned_count"] == 0
    assert len(client.snapshot_calls) == 1
    assert report["errors"] == [
        {
            "category": "permission_denied",
            "code": "permission_denied",
            "operation": "get_market_snapshot",
            "message": "no quote right",
        }
    ]


def test_market_state_failure_does_not_trigger_extra_snapshot_calls() -> None:
    securities = make_securities()
    client = FakeClient(
        securities,
        state_error=TimeoutError("market-state timeout"),
    )

    report = run_fixed_probe(client, securities)

    assert report["status"] == "partial"
    assert len(client.market_state_calls) == 1
    assert len(client.snapshot_calls) == 1
    assert report["market_state"] == "UNKNOWN"
    assert report["live_freshness_verified"] is False
    assert report_errors(report)[0]["category"] == "timeout"


def test_sensitive_error_content_is_cleaned() -> None:
    message = (
        f"\x00\ue000account=123456 phone=13800138000 email=user@example.com "
        f"token={SECRET} authorization=Bearer {SECRET}"
    )

    sanitized = sanitize_error_message(message)

    assert SECRET not in sanitized
    assert "123456" not in sanitized
    assert "13800138000" not in sanitized
    assert "user@example.com" not in sanitized
    assert "\x00" not in sanitized
    assert "\ue000" not in sanitized
    assert "[redacted-account]" in sanitized
    assert "[redacted-secret]" in sanitized


def test_report_contains_no_dataframe_or_client_objects() -> None:
    securities = make_securities()
    report = run_fixed_probe(FakeClient(securities), securities)

    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)

    assert "FakeClient" not in serialized
    assert "FakeTable" not in serialized
    assert "DataFrame" not in serialized


def test_output_is_stable_for_fixed_inputs() -> None:
    securities = make_securities()

    first = run_fixed_probe(FakeClient(securities), securities)
    second = run_fixed_probe(FakeClient(tuple(reversed(securities))), securities)

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_dry_run_uses_real_count_without_importing_or_connecting(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    securities = make_securities()
    config_path = tmp_path / "watchlist.yaml"
    write_watchlist(config_path, securities)

    def fail_if_client_is_created(host: str, port: int) -> FakeClient:
        raise AssertionError(f"dry-run attempted a connection to {host}:{port}")

    exit_code = main(
        ["--dry-run", "--config", str(config_path)],
        project_root=tmp_path,
        client_factory=fail_if_client_is_created,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload == {
        "status": "dry_run",
        "requested_count": 93,
        "stock_count": 79,
        "etf_count": 14,
        "planned_snapshot_calls": 1,
        "planned_market_state_calls": 1,
        "normalized_sample_symbols": {
            "000333.SZ": "SZ.000333",
            "159949.SZ": "SZ.159949",
            "588200.SH": "SH.588200",
            "600183.SH": "SH.600183",
        },
        "host": "127.0.0.1",
        "port": 11111,
        "network_calls": 0,
    }


def test_connection_failure_writes_sanitized_failed_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    securities = make_securities()
    config_path = tmp_path / "watchlist.yaml"
    write_watchlist(config_path, securities)

    def unavailable(host: str, port: int) -> FakeClient:
        raise ConnectionRefusedError(
            f"OpenD is not started account=123456 token={SECRET}"
        )

    exit_code = main(
        ["--execute", "--config", str(config_path)],
        project_root=tmp_path,
        client_factory=unavailable,
        endpoint_checker=lambda host, port, timeout: None,
        now=lambda: FIXED_RECEIVED_AT,
    )

    output = capsys.readouterr().out
    report_text = (
        tmp_path
        / "data/spikes/opend_quote/a-share-realtime-capabilities.json"
    ).read_text(encoding="utf-8")
    report = json.loads(report_text)
    assert exit_code == 2
    assert report["status"] == "failed"
    assert report["errors"][0]["category"] == "connection_refused"
    assert report["network_calls"] == 0
    assert SECRET not in output
    assert SECRET not in report_text
    assert "123456" not in report_text


def test_refused_endpoint_fails_once_with_short_timeout() -> None:
    connector_calls: list[tuple[tuple[str, int], float]] = []

    def refused_connector(
        address: tuple[str, int],
        *,
        timeout: float,
    ) -> Never:
        connector_calls.append((address, timeout))
        raise ConnectionRefusedError("ECONNREFUSED")

    with pytest.raises(OpenDProbeError) as captured:
        check_opend_endpoint(
            "127.0.0.1",
            11111,
            connector=refused_connector,
        )

    assert DEFAULT_CONNECT_TIMEOUT_SECONDS <= 2
    assert connector_calls == [
        (("127.0.0.1", 11111), DEFAULT_CONNECT_TIMEOUT_SECONDS)
    ]
    assert captured.value.category is ErrorCategory.CONNECTION_REFUSED
    assert captured.value.operation == "endpoint_preflight"


def test_refused_endpoint_does_not_construct_sdk_context_or_repeat_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    securities = make_securities()
    config_path = tmp_path / "watchlist.yaml"
    write_watchlist(config_path, securities)
    client_factory_calls = 0
    endpoint_calls = 0

    def refused_endpoint(host: str, port: int, timeout: float) -> None:
        nonlocal endpoint_calls
        endpoint_calls += 1
        assert (host, port) == ("127.0.0.1", 11111)
        assert timeout <= 2
        raise ConnectionRefusedError("ECONNREFUSED")

    def must_not_construct_context(host: str, port: int) -> FakeClient:
        nonlocal client_factory_calls
        client_factory_calls += 1
        raise AssertionError(f"constructed SDK context for {host}:{port}")

    exit_code = main(
        ["--execute", "--config", str(config_path)],
        project_root=tmp_path,
        client_factory=must_not_construct_context,
        endpoint_checker=refused_endpoint,
        now=lambda: FIXED_RECEIVED_AT,
    )

    terminal_lines = capsys.readouterr().out.splitlines()
    summary = json.loads(terminal_lines[0])
    report_path = (
        tmp_path
        / "data/spikes/opend_quote/a-share-realtime-capabilities.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code == 2
    assert endpoint_calls == 1
    assert client_factory_calls == 0
    assert len(terminal_lines) == 1
    assert summary["error_counts"] == {"connection_refused": 1}
    assert summary["host"] == "127.0.0.1"
    assert summary["port"] == 11111
    assert summary["recommendation"] == OPEND_START_RECOMMENDATION
    assert summary["endpoint_reachable"] is False
    assert summary["endpoint_preflight_calls"] == 1
    assert "ECONNREFUSED" not in terminal_lines[0]
    assert report["errors"][0]["category"] == "connection_refused"
    assert report["recommendation"] == OPEND_START_RECOMMENDATION
    assert report["network_calls"] == 0


def test_reachable_endpoint_constructs_context_once_and_always_closes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    securities = make_securities()
    config_path = tmp_path / "watchlist.yaml"
    write_watchlist(config_path, securities)
    client = FakeClient(securities)
    endpoint_calls = 0
    factory_calls = 0

    def reachable_endpoint(host: str, port: int, timeout: float) -> None:
        nonlocal endpoint_calls
        endpoint_calls += 1
        assert (host, port, timeout) == (
            "127.0.0.1",
            11111,
            DEFAULT_CONNECT_TIMEOUT_SECONDS,
        )

    def create_client(host: str, port: int) -> FakeClient:
        nonlocal factory_calls
        factory_calls += 1
        return client

    exit_code = main(
        ["--execute", "--config", str(config_path)],
        project_root=tmp_path,
        client_factory=create_client,
        endpoint_checker=reachable_endpoint,
        now=lambda: FIXED_RECEIVED_AT,
    )

    summary = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert endpoint_calls == 1
    assert factory_calls == 1
    assert client.close_calls == 1
    assert summary["endpoint_reachable"] is True
    assert summary["endpoint_preflight_calls"] == 1


def test_context_closes_when_probe_raises(
    tmp_path: Path,
) -> None:
    securities = make_securities()
    config_path = tmp_path / "watchlist.yaml"
    write_watchlist(config_path, securities)
    client = FakeClient(securities)

    def failing_clock() -> Never:
        raise ProbeConfigurationError("fixture clock failed")

    with pytest.raises(ProbeConfigurationError):
        main(
            ["--execute", "--config", str(config_path)],
            project_root=tmp_path,
            client_factory=lambda host, port: client,
            endpoint_checker=lambda host, port, timeout: None,
            now=failing_clock,
        )

    assert client.close_calls == 1


def test_spike_report_directory_is_git_ignored() -> None:
    ignored = subprocess.run(
        [
            "git",
            "check-ignore",
            "--no-index",
            "--quiet",
            "data/spikes/opend_quote/a-share-realtime-capabilities.json",
        ],
        cwd=PROJECT_ROOT,
        check=False,
    )

    assert ignored.returncode == 0
