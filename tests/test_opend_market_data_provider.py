from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Never

import pytest

from market_sentinel.config import Settings
from market_sentinel.domain.models import MarketPhase
from market_sentinel.domain.quotes import (
    MarketQuote,
    QualitySeverity,
    QuoteFreshness,
    QuoteMarketState,
    TradingStatus,
)
from market_sentinel.domain.security_data import (
    DataCompleteness,
    MarketDataErrorCategory,
    SecurityCategory,
)
from market_sentinel.market_data.errors import MarketDataQualityError
from market_sentinel.market_data.opend import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    FutuOpenDQuoteClient,
    OpenDMarketDataProvider,
    classify_opend_error,
    internal_to_opend_symbol,
    opend_to_internal_symbol,
    sanitize_opend_error,
)

FIXED_NOW = datetime(2026, 7, 27, 2, 0, 1, tzinfo=UTC)
PHASE = MarketPhase.A_SHARE_MIDDAY
SECRET = "sensitive-token-value"


def make_security_types() -> dict[str, SecurityCategory]:
    securities: dict[str, SecurityCategory] = {
        "600183.SH": SecurityCategory.STOCK,
        "000333.SZ": SecurityCategory.STOCK,
    }
    for offset in range(77):
        securities[f"{601000 + offset:06d}.SH"] = SecurityCategory.STOCK
    securities.update(
        {
            "588200.SH": SecurityCategory.ETF,
            "510300.SH": SecurityCategory.ETF,
            "159949.SZ": SecurityCategory.ETF,
        }
    )
    for offset in range(11):
        securities[f"{510500 + offset:06d}.SH"] = SecurityCategory.ETF
    assert len(securities) == 93
    assert sum(value is SecurityCategory.STOCK for value in securities.values()) == 79
    assert sum(value is SecurityCategory.ETF for value in securities.values()) == 14
    return dict(sorted(securities.items()))


def snapshot_row(
    symbol: str,
    *,
    update_time: object = "2026-07-27 10:00:00",
    suspension: object = False,
) -> dict[str, object]:
    return {
        "code": internal_to_opend_symbol(symbol),
        "update_time": update_time,
        "last_price": "10.20",
        "prev_close_price": "10.00",
        "open_price": "10.10",
        "high_price": "10.50",
        "low_price": "9.90",
        "volume": "1000",
        "turnover": "10200.50",
        "suspension": suspension,
        "sec_status": "NORMAL",
    }


def state_rows(
    symbols: Sequence[str],
    state: str = "MORNING",
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "code": internal_to_opend_symbol(symbol),
            "market_state": state,
        }
        for symbol in symbols
    )


class FakeClient:
    def __init__(
        self,
        symbols: Sequence[str],
        *,
        state: str = "MORNING",
        snapshots: Sequence[Mapping[str, object]] | None = None,
        state_error: BaseException | None = None,
        snapshot_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.states = state_rows(symbols, state)
        self.snapshots = tuple(
            dict(row)
            for row in (
                snapshots
                if snapshots is not None
                else [snapshot_row(symbol) for symbol in symbols]
            )
        )
        self.state_error = state_error
        self.snapshot_error = snapshot_error
        self.close_error = close_error
        self.state_calls: list[tuple[str, ...]] = []
        self.snapshot_calls: list[tuple[str, ...]] = []
        self.close_calls = 0

    def get_market_state(
        self,
        provider_symbols: Sequence[str],
    ) -> tuple[dict[str, object], ...]:
        self.state_calls.append(tuple(provider_symbols))
        if self.state_error is not None:
            raise self.state_error
        return self.states

    def get_market_snapshot(
        self,
        provider_symbols: Sequence[str],
    ) -> tuple[dict[str, object], ...]:
        self.snapshot_calls.append(tuple(provider_symbols))
        if self.snapshot_error is not None:
            raise self.snapshot_error
        return self.snapshots

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class FakeTable:
    def __init__(self, records: Sequence[Mapping[str, object]]) -> None:
        self.records = [dict(record) for record in records]
        self.to_dict_calls = 0

    def to_dict(self, *, orient: str) -> list[dict[str, object]]:
        assert orient == "records"
        self.to_dict_calls += 1
        return [dict(record) for record in self.records]


class FakeOpenQuoteContext:
    def __init__(
        self,
        state_table: FakeTable,
        snapshot_table: FakeTable,
    ) -> None:
        self.state_table = state_table
        self.snapshot_table = snapshot_table
        self.close_calls = 0

    def get_market_state(self, symbols: Sequence[str]) -> tuple[int, FakeTable]:
        return 0, self.state_table

    def get_market_snapshot(self, symbols: Sequence[str]) -> tuple[int, FakeTable]:
        return 0, self.snapshot_table

    def close(self) -> None:
        self.close_calls += 1


class FakeFutuModule:
    RET_OK = 0

    def __init__(self, context: FakeOpenQuoteContext) -> None:
        self.context = context
        self.context_calls: list[tuple[str, int]] = []

    def OpenQuoteContext(
        self,
        *,
        host: str,
        port: int,
    ) -> FakeOpenQuoteContext:
        self.context_calls.append((host, port))
        return self.context


def build_provider(
    client: FakeClient,
    security_types: Mapping[str, SecurityCategory],
    *,
    endpoint_checker: Callable[[str, int, float], None] | None = None,
    now: Callable[[], datetime] = lambda: FIXED_NOW,
) -> OpenDMarketDataProvider:
    return OpenDMarketDataProvider(
        security_types,
        client_factory=lambda host, port: client,
        endpoint_checker=endpoint_checker or (lambda host, port, timeout: None),
        now=now,
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
def test_symbols_convert_and_validate_bidirectionally(
    internal: str,
    provider: str,
) -> None:
    assert internal_to_opend_symbol(internal) == provider
    assert opend_to_internal_symbol(provider) == internal


@pytest.mark.parametrize("symbol", ["SH.600183", "600183.XSHG", "bad"])
def test_internal_symbol_rejects_noncanonical_input(symbol: str) -> None:
    with pytest.raises(MarketDataQualityError):
        internal_to_opend_symbol(symbol)


async def test_all_93_symbols_use_one_snapshot_and_one_state_call() -> None:
    security_types = make_security_types()
    symbols = tuple(security_types)
    client = FakeClient(symbols)
    provider = build_provider(client, security_types)

    batch = await provider.get_quotes(tuple(reversed(symbols)), PHASE)

    expected = tuple(internal_to_opend_symbol(symbol) for symbol in symbols)
    assert client.snapshot_calls == [expected]
    assert client.state_calls == [expected]
    assert client.close_calls == 1
    assert batch.completeness is DataCompleteness.COMPLETE
    assert len(batch.quotes) == 93
    assert batch.returned_count == 93
    assert batch.snapshot_calls == 1
    assert batch.market_state_calls == 1
    assert batch.network_calls == 2
    assert tuple(quote.symbol for quote in batch.quotes) == symbols


async def test_shanghai_shenzhen_stocks_and_etfs_map_to_quotes() -> None:
    security_types = {
        "600183.SH": SecurityCategory.STOCK,
        "000333.SZ": SecurityCategory.STOCK,
        "588200.SH": SecurityCategory.ETF,
        "159949.SZ": SecurityCategory.ETF,
    }
    symbols = tuple(security_types)
    batch = await build_provider(
        FakeClient(symbols),
        security_types,
    ).get_quotes(symbols, PHASE)
    quotes = {quote.symbol: quote for quote in batch.quotes}

    assert quotes["600183.SH"].security_type is SecurityCategory.STOCK
    assert quotes["000333.SZ"].security_type is SecurityCategory.STOCK
    assert quotes["588200.SH"].security_type is SecurityCategory.ETF
    assert quotes["159949.SZ"].security_type is SecurityCategory.ETF
    assert quotes["600183.SH"].provider_symbol == "SH.600183"
    assert quotes["000333.SZ"].provider_symbol == "SZ.000333"


async def test_dataframe_is_converted_inside_sdk_boundary() -> None:
    symbols = ("600183.SH",)
    state_table = FakeTable(state_rows(symbols))
    snapshot_table = FakeTable([snapshot_row(symbols[0])])
    context = FakeOpenQuoteContext(state_table, snapshot_table)
    module = FakeFutuModule(context)
    provider = OpenDMarketDataProvider(
        {"600183.SH": SecurityCategory.STOCK},
        client_factory=lambda host, port: FutuOpenDQuoteClient(
            host,
            port,
            futu_module=module,
        ),
        endpoint_checker=lambda host, port, timeout: None,
        now=lambda: FIXED_NOW,
    )

    batch = await provider.get_quotes(symbols, PHASE)

    assert batch.completeness is DataCompleteness.COMPLETE
    assert isinstance(batch.quotes[0], MarketQuote)
    assert not isinstance(batch.quotes[0], FakeTable)
    assert state_table.to_dict_calls == snapshot_table.to_dict_calls == 1
    assert module.context_calls == [("127.0.0.1", 11111)]
    assert context.close_calls == 1


async def test_decimal_time_and_units_are_normalized() -> None:
    symbol = "600183.SH"
    batch = await build_provider(
        FakeClient((symbol,)),
        {symbol: SecurityCategory.STOCK},
    ).get_quotes((symbol,), PHASE)
    quote = batch.quotes[0]

    assert quote.previous_close == Decimal("10.00")
    assert quote.open == Decimal("10.10")
    assert quote.high == Decimal("10.50")
    assert quote.low == Decimal("9.90")
    assert quote.last == Decimal("10.20")
    assert quote.volume == 1000
    assert quote.turnover == Decimal("10200.50")
    assert quote.source_time.isoformat() == "2026-07-27T10:00:00+08:00"
    assert quote.received_at == FIXED_NOW
    assert quote.received_at.tzinfo is UTC
    assert quote.delay_seconds == Decimal("1.0")


async def test_missing_ordinary_symbol_is_partial() -> None:
    security_types = {
        "600183.SH": SecurityCategory.STOCK,
        "000333.SZ": SecurityCategory.STOCK,
    }
    client = FakeClient(
        tuple(security_types),
        snapshots=[snapshot_row("600183.SH")],
    )

    batch = await build_provider(client, security_types).get_quotes(
        tuple(security_types),
        PHASE,
    )

    assert batch.completeness is DataCompleteness.PARTIAL
    assert batch.missing_symbols == ("000333.SZ",)
    assert tuple(quote.symbol for quote in batch.quotes) == ("600183.SH",)


async def test_missing_critical_holding_is_partial_with_critical_warning() -> None:
    security_types = {
        "588200.SH": SecurityCategory.ETF,
        "600183.SH": SecurityCategory.STOCK,
    }
    client = FakeClient(
        tuple(security_types),
        snapshots=[snapshot_row("600183.SH")],
    )

    batch = await build_provider(client, security_types).get_quotes(
        tuple(security_types),
        PHASE,
    )

    assert batch.completeness is DataCompleteness.PARTIAL
    assert batch.critical_missing_symbols == ("588200.SH",)
    issue = next(
        issue for issue in batch.quality_issues if issue.symbol == "588200.SH"
    )
    assert issue.code == "critical_symbol_unavailable"
    assert issue.severity is QualitySeverity.CRITICAL


async def test_duplicate_symbol_is_rejected_without_selecting_a_row() -> None:
    symbols = ("600183.SH", "000333.SZ")
    rows = [
        snapshot_row("600183.SH"),
        snapshot_row("600183.SH"),
        snapshot_row("000333.SZ"),
    ]
    client = FakeClient(symbols, snapshots=rows)
    provider = build_provider(
        client,
        {symbol: SecurityCategory.STOCK for symbol in symbols},
    )

    batch = await provider.get_quotes(symbols, PHASE)

    assert batch.completeness is DataCompleteness.PARTIAL
    assert batch.duplicate_symbols == ("600183.SH",)
    assert batch.invalid_symbols == ("600183.SH",)
    assert tuple(quote.symbol for quote in batch.quotes) == ("000333.SZ",)


async def test_unexpected_symbol_is_recorded_but_not_returned() -> None:
    symbol = "600183.SH"
    client = FakeClient(
        (symbol,),
        snapshots=[snapshot_row(symbol), snapshot_row("600999.SH")],
    )

    batch = await build_provider(
        client,
        {symbol: SecurityCategory.STOCK},
    ).get_quotes((symbol,), PHASE)

    assert batch.completeness is DataCompleteness.PARTIAL
    assert batch.unexpected_symbols == ("600999.SH",)
    assert tuple(quote.symbol for quote in batch.quotes) == (symbol,)


async def test_suspended_quote_is_usable_with_warning() -> None:
    symbol = "600183.SH"
    row = snapshot_row(symbol, suspension=True)
    for field in ("last_price", "open_price", "high_price", "low_price"):
        row[field] = "0"
    row["volume"] = "0"
    row["turnover"] = "0"
    batch = await build_provider(
        FakeClient((symbol,), snapshots=[row]),
        {symbol: SecurityCategory.STOCK},
    ).get_quotes((symbol,), PHASE)

    assert batch.completeness is DataCompleteness.COMPLETE
    assert batch.quotes[0].trading_status is TradingStatus.SUSPENDED
    assert batch.quotes[0].last is None
    assert any(issue.code == "suspended" for issue in batch.quality_issues)


@pytest.mark.parametrize(
    ("state", "expected_state", "expected_freshness", "expected_status"),
    [
        (
            "CLOSED",
            QuoteMarketState.CLOSED,
            QuoteFreshness.OUTSIDE_CONTINUOUS_TRADING,
            TradingStatus.CLOSED,
        ),
        (
            "REST",
            QuoteMarketState.MIDDAY_BREAK,
            QuoteFreshness.OUTSIDE_CONTINUOUS_TRADING,
            TradingStatus.CLOSED,
        ),
        (
            "MORNING",
            QuoteMarketState.CONTINUOUS_TRADING,
            QuoteFreshness.NOT_VERIFIED_CONTINUOUS_TRADING,
            TradingStatus.TRADING,
        ),
        (
            "AFTERNOON",
            QuoteMarketState.CONTINUOUS_TRADING,
            QuoteFreshness.NOT_VERIFIED_CONTINUOUS_TRADING,
            TradingStatus.TRADING,
        ),
        (
            "AFTER_HOURS_BEGIN",
            QuoteMarketState.CLOSED,
            QuoteFreshness.OUTSIDE_CONTINUOUS_TRADING,
            TradingStatus.CLOSED,
        ),
        (
            "AFTER_HOURS_END",
            QuoteMarketState.CLOSED,
            QuoteFreshness.OUTSIDE_CONTINUOUS_TRADING,
            TradingStatus.CLOSED,
        ),
        (
            "STIB_AFTER_HOURS_BEGIN",
            QuoteMarketState.CLOSED,
            QuoteFreshness.OUTSIDE_CONTINUOUS_TRADING,
            TradingStatus.CLOSED,
        ),
        (
            "MarketState.STIB_AFTER_HOURS_END",
            QuoteMarketState.CLOSED,
            QuoteFreshness.OUTSIDE_CONTINUOUS_TRADING,
            TradingStatus.CLOSED,
        ),
        (
            "PRE_MARKET_BEGIN",
            QuoteMarketState.AUCTION,
            QuoteFreshness.OUTSIDE_CONTINUOUS_TRADING,
            TradingStatus.AUCTION,
        ),
        (
            "UNRECOGNIZED",
            QuoteMarketState.UNKNOWN,
            QuoteFreshness.UNKNOWN_MARKET_STATE,
            TradingStatus.UNKNOWN,
        ),
    ],
)
async def test_market_state_does_not_claim_unverified_live_freshness(
    state: str,
    expected_state: QuoteMarketState,
    expected_freshness: QuoteFreshness,
    expected_status: TradingStatus,
) -> None:
    symbol = "600183.SH"
    batch = await build_provider(
        FakeClient((symbol,), state=state),
        {symbol: SecurityCategory.STOCK},
    ).get_quotes((symbol,), PHASE)

    assert batch.completeness is DataCompleteness.COMPLETE
    assert batch.market_state is expected_state
    assert batch.freshness is expected_freshness
    assert batch.quotes[0].trading_status is expected_status


async def test_closed_snapshot_with_large_delay_remains_complete() -> None:
    symbol = "600183.SH"
    received_at = datetime(2026, 7, 27, 15, 21, tzinfo=UTC)
    batch = await build_provider(
        FakeClient(
            (symbol,),
            state="MarketState.STIB_AFTER_HOURS_END\x00",
            snapshots=[
                snapshot_row(
                    symbol,
                    update_time="2026-07-27 15:00:00",
                )
            ],
        ),
        {symbol: SecurityCategory.STOCK},
        now=lambda: received_at,
    ).get_quotes((symbol,), PHASE)

    assert batch.completeness is DataCompleteness.COMPLETE
    assert batch.invalid_symbols == ()
    assert batch.market_state is QuoteMarketState.CLOSED
    assert batch.freshness is QuoteFreshness.OUTSIDE_CONTINUOUS_TRADING
    assert batch.quotes[0].delay_seconds > Decimal(30000)
    assert batch.raw_market_states == ("STIB_AFTER_HOURS_END",)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ({"update_time": None}, "source_time_missing"),
        ({"update_time": "2026-07-27 10:00:02"}, "future_source_time"),
        ({"high_price": "10.00"}, "invalid_price_relationship"),
        ({"volume": "-1"}, "invalid_volume"),
        ({"turnover": "-0.01"}, "invalid_turnover"),
        ({"prev_close_price": "0"}, "invalid_price_relationship"),
    ],
)
async def test_invalid_quote_is_rejected_by_quality_gate(
    mutation: Mapping[str, object],
    expected_code: str,
) -> None:
    symbols = ("600183.SH", "000333.SZ")
    invalid_row = snapshot_row("600183.SH")
    invalid_row.update(mutation)
    client = FakeClient(
        symbols,
        snapshots=[invalid_row, snapshot_row("000333.SZ")],
    )
    provider = build_provider(
        client,
        {symbol: SecurityCategory.STOCK for symbol in symbols},
    )

    batch = await provider.get_quotes(symbols, PHASE)

    assert batch.completeness is DataCompleteness.PARTIAL
    assert batch.invalid_symbols == ("600183.SH",)
    assert expected_code in {
        issue.code for issue in batch.quality_issues if issue.symbol == "600183.SH"
    }


async def test_all_invalid_quotes_fail_closed() -> None:
    symbols = ("600183.SH", "000333.SZ")
    rows = [
        snapshot_row(symbol, update_time="2026-07-27 10:00:02")
        for symbol in symbols
    ]
    batch = await build_provider(
        FakeClient(symbols, snapshots=rows),
        {symbol: SecurityCategory.STOCK for symbol in symbols},
    ).get_quotes(symbols, PHASE)

    assert batch.completeness is DataCompleteness.FAILED
    assert batch.quotes == ()
    assert batch.invalid_symbols == tuple(sorted(symbols))


async def test_endpoint_refusal_fails_before_client_construction() -> None:
    symbol = "600183.SH"
    factory_calls = 0
    endpoint_calls: list[tuple[str, int, float]] = []

    def refused(host: str, port: int, timeout: float) -> Never:
        endpoint_calls.append((host, port, timeout))
        raise ConnectionRefusedError("ECONNREFUSED")

    def forbidden_factory(host: str, port: int) -> Never:
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("OpenD context must not be constructed")

    provider = OpenDMarketDataProvider(
        {symbol: SecurityCategory.STOCK},
        client_factory=forbidden_factory,
        endpoint_checker=refused,
        now=lambda: FIXED_NOW,
    )

    batch = await provider.get_quotes((symbol,), PHASE)

    assert batch.completeness is DataCompleteness.FAILED
    assert endpoint_calls == [
        ("127.0.0.1", 11111, DEFAULT_CONNECT_TIMEOUT_SECONDS)
    ]
    assert factory_calls == 0
    assert batch.provider_errors[0].category is MarketDataErrorCategory.CONNECTION_REFUSED
    assert batch.provider_errors[0].code == "connection_refused"


async def test_unavailable_endpoint_is_a_structured_failed_batch() -> None:
    symbol = "600183.SH"

    def unavailable(host: str, port: int, timeout: float) -> Never:
        raise OSError("OpenD is not started")

    provider = OpenDMarketDataProvider(
        {symbol: SecurityCategory.STOCK},
        endpoint_checker=unavailable,
        now=lambda: FIXED_NOW,
    )

    batch = await provider.get_quotes((symbol,), PHASE)

    assert batch.completeness is DataCompleteness.FAILED
    assert batch.provider_errors[0].category is MarketDataErrorCategory.OPEND_UNAVAILABLE
    assert batch.provider_errors[0].code == "opend_unavailable"


@pytest.mark.parametrize(
    ("error", "category", "code"),
    [
        (
            RuntimeError("no quote right"),
            MarketDataErrorCategory.PERMISSION_DENIED,
            "permission_denied",
        ),
        (
            RuntimeError("qot login failed"),
            MarketDataErrorCategory.AUTHENTICATION_FAILED,
            "authentication_failed",
        ),
        (TimeoutError("timed out"), MarketDataErrorCategory.TIMEOUT, "timeout"),
        (
            classify_opend_error(
                "malformed protocol response",
                operation="get_market_snapshot",
            ),
            MarketDataErrorCategory.PROTOCOL,
            "protocol_error",
        ),
        (
            RuntimeError("frequency limit exceeded"),
            MarketDataErrorCategory.RATE_LIMIT,
            "rate_limited",
        ),
        (
            ValueError("bad SDK payload"),
            MarketDataErrorCategory.INVALID_RESPONSE,
            "invalid_response",
        ),
        (
            RuntimeError("unknown SDK failure"),
            MarketDataErrorCategory.UNEXPECTED,
            "unexpected_error",
        ),
    ],
)
async def test_whole_snapshot_errors_fail_with_precise_category(
    error: BaseException,
    category: MarketDataErrorCategory,
    code: str,
) -> None:
    symbol = "600183.SH"
    client = FakeClient((symbol,), snapshot_error=error)
    batch = await build_provider(
        client,
        {symbol: SecurityCategory.STOCK},
    ).get_quotes((symbol,), PHASE)

    assert batch.completeness is DataCompleteness.FAILED
    assert batch.quotes == ()
    assert batch.provider_errors[0].category is category
    assert batch.provider_errors[0].code == code
    assert client.close_calls == 1


async def test_market_state_error_keeps_quotes_but_is_partial_and_unknown() -> None:
    symbol = "600183.SH"
    client = FakeClient((symbol,), state_error=TimeoutError("state timeout"))
    batch = await build_provider(
        client,
        {symbol: SecurityCategory.STOCK},
    ).get_quotes((symbol,), PHASE)

    assert batch.completeness is DataCompleteness.PARTIAL
    assert len(batch.quotes) == 1
    assert batch.market_state is QuoteMarketState.UNKNOWN
    assert batch.freshness is QuoteFreshness.UNKNOWN_MARKET_STATE
    assert batch.provider_errors[0].category is MarketDataErrorCategory.TIMEOUT


async def test_context_closes_when_clock_raises_during_conversion() -> None:
    symbol = "600183.SH"
    client = FakeClient((symbol,))
    clock_calls = 0

    def failing_clock() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        if clock_calls > 1:
            raise MarketDataQualityError("fixture clock failed")
        return FIXED_NOW

    provider = OpenDMarketDataProvider(
        {symbol: SecurityCategory.STOCK},
        client_factory=lambda host, port: client,
        endpoint_checker=lambda host, port, timeout: None,
        now=failing_clock,
    )

    with pytest.raises(MarketDataQualityError, match="fixture clock failed"):
        await provider.get_quotes((symbol,), PHASE)

    assert client.close_calls == 1


def test_sensitive_errors_remove_credentials_and_sdk_repr() -> None:
    message = (
        f"\x00account=123456 phone=13800138000 email=user@example.com "
        f"token={SECRET} cookie={SECRET} "
        "<futu.quote.open_quote_context.OpenQuoteContext object at 0x1234abcd>"
    )

    sanitized = sanitize_opend_error(message)

    assert SECRET not in sanitized
    assert "123456" not in sanitized
    assert "13800138000" not in sanitized
    assert "user@example.com" not in sanitized
    assert "0x1234abcd" not in sanitized
    assert "OpenQuoteContext object" not in sanitized
    assert "[redacted-secret]" in sanitized
    assert "[redacted-sdk-object]" in sanitized


async def test_domain_result_contains_no_dataframe_or_sdk_objects() -> None:
    symbol = "600183.SH"
    batch = await build_provider(
        FakeClient((symbol,)),
        {symbol: SecurityCategory.STOCK},
    ).get_quotes((symbol,), PHASE)

    serialized = batch.model_dump_json()

    assert "DataFrame" not in serialized
    assert "FakeClient" not in serialized
    assert "OpenQuoteContext" not in serialized


async def test_output_is_stable_for_different_request_order() -> None:
    symbols = ("600183.SH", "000333.SZ", "588200.SH", "159949.SZ")
    security_types = {
        symbol: SecurityCategory.ETF
        if symbol in {"588200.SH", "159949.SZ"}
        else SecurityCategory.STOCK
        for symbol in symbols
    }
    first = await build_provider(
        FakeClient(symbols),
        security_types,
    ).get_quotes(symbols, PHASE)
    second = await build_provider(
        FakeClient(tuple(reversed(symbols))),
        security_types,
    ).get_quotes(tuple(reversed(symbols)), PHASE)

    assert json.dumps(first.model_dump(mode="json"), sort_keys=True) == json.dumps(
        second.model_dump(mode="json"),
        sort_keys=True,
    )


def test_provider_construction_does_not_connect() -> None:
    client_calls = 0
    endpoint_calls = 0

    def client_factory(host: str, port: int) -> Never:
        nonlocal client_calls
        client_calls += 1
        raise AssertionError("client construction must be lazy")

    def endpoint_checker(host: str, port: int, timeout: float) -> None:
        nonlocal endpoint_calls
        endpoint_calls += 1

    OpenDMarketDataProvider(
        {"600183.SH": SecurityCategory.STOCK},
        client_factory=client_factory,
        endpoint_checker=endpoint_checker,
    )

    assert client_calls == 0
    assert endpoint_calls == 0


def test_opend_settings_are_safe_and_bounded() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        opend_host="localhost",
        opend_port=21111,
        opend_connect_timeout_seconds=1.5,
    )

    assert settings.opend_host == "localhost"
    assert settings.opend_port == 21111
    assert settings.opend_connect_timeout_seconds == 1.5
    assert "password" not in Settings.model_fields
    assert "token" not in " ".join(
        field for field in Settings.model_fields if field.startswith("opend_")
    )


def test_opend_is_an_optional_dependency() -> None:
    pyproject = (
        Path(__file__).resolve().parents[1] / "pyproject.toml"
    ).read_text(encoding="utf-8")

    assert 'opend = [' in pyproject
    assert '"futu-api>=10.9.6908,<11"' in pyproject
    default_dependencies = pyproject.split("[project.optional-dependencies]", 1)[0]
    assert "futu-api" not in default_dependencies


async def test_more_than_400_symbols_is_rejected_without_network() -> None:
    symbols = tuple(f"{index:06d}.SZ" for index in range(401))
    endpoint_calls = 0

    def endpoint_checker(host: str, port: int, timeout: float) -> None:
        nonlocal endpoint_calls
        endpoint_calls += 1

    provider = OpenDMarketDataProvider(
        {symbol: SecurityCategory.STOCK for symbol in symbols},
        endpoint_checker=endpoint_checker,
    )

    with pytest.raises(MarketDataQualityError, match="limit is 400"):
        await provider.get_quotes(symbols, PHASE)

    assert endpoint_calls == 0
