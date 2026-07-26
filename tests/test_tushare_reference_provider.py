from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

import pytest
from pydantic import SecretStr

from market_sentinel.config import Settings
from market_sentinel.domain.security_data import (
    AdjustmentMode,
    DataCompleteness,
    MarketDataErrorCategory,
    SecurityCategory,
    SecurityExchange,
    TurnoverUnit,
    VolumeUnit,
)
from market_sentinel.market_data.errors import MarketDataProviderError
from market_sentinel.market_data.tushare import (
    TUSHARE_SOURCE,
    TushareDailyBarProvider,
    TushareSecurityMasterProvider,
    build_tushare_reference_providers,
)

TOKEN = "test-token-value-that-must-never-appear"
FIXED_NOW = datetime(2026, 7, 24, 8, 0, tzinfo=UTC)
TRADE_DATE = date(2026, 7, 24)


class FakeTable:
    def __init__(
        self,
        records: Sequence[Mapping[str, object]],
        *,
        columns: Sequence[str] | None = None,
    ) -> None:
        self._records = [dict(record) for record in records]
        self.columns = list(
            columns if columns is not None else self._records[0] if self._records else ()
        )

    def to_dict(self, *, orient: str) -> list[dict[str, object]]:
        assert orient == "records"
        return [dict(record) for record in self._records]


class FakeClient:
    def __init__(
        self,
        outcomes: Mapping[str, object | BaseException | Callable[..., object]],
    ) -> None:
        self._outcomes = dict(outcomes)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __getattr__(self, api_name: str) -> Callable[..., object]:
        def call(**kwargs: object) -> object:
            self.calls.append((api_name, dict(kwargs)))
            outcome = self._outcomes.get(api_name, FakeTable([]))
            if isinstance(outcome, BaseException):
                raise outcome
            if callable(outcome):
                return outcome(**kwargs)
            return outcome

        return call


class FakeTushareModule:
    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.pro_api_calls: list[str] = []

    def pro_api(self, token: str) -> FakeClient:
        self.pro_api_calls.append(token)
        return self.client

    def set_token(self, token: str) -> None:
        raise AssertionError(f"set_token must not be called: {token}")


def stock_basic_row(symbol: str) -> dict[str, object]:
    return {
        "ts_code": symbol,
        "symbol": symbol.removesuffix(".SH").removesuffix(".SZ"),
        "name": f"fixture-{symbol}",
        "market": "主板",
        "exchange": "SSE" if symbol.endswith(".SH") else "SZSE",
        "curr_type": "CNY",
        "list_status": "L",
        "list_date": "20000101",
    }


def index_basic_row(symbol: str = "000001.SH") -> dict[str, object]:
    return {
        "ts_code": symbol,
        "name": "上证指数",
        "fullname": "上证综合指数",
        "market": "SSE",
        "publisher": "SSE",
        "index_type": "综合指数",
        "category": "综合指数",
        "list_date": "19910715",
    }


def daily_row(
    symbol: str,
    *,
    volume: object = "12.34",
    turnover: object = "56.789",
) -> dict[str, object]:
    return {
        "ts_code": symbol,
        "trade_date": "20260724",
        "open": "10.10",
        "high": "10.80",
        "low": "9.90",
        "close": "10.50",
        "pre_close": "10.00",
        "vol": volume,
        "amount": turnover,
    }


@pytest.mark.asyncio
async def test_stock_basic_converts_shanghai_and_shenzhen_records() -> None:
    def response(**kwargs: object) -> FakeTable:
        return FakeTable([stock_basic_row(cast(str, kwargs["ts_code"]))])

    client = FakeClient({"stock_basic": response})
    provider = TushareSecurityMasterProvider(
        client,
        {
            "600183.SH": SecurityCategory.STOCK,
            "000333.SZ": SecurityCategory.STOCK,
        },
        now=lambda: FIXED_NOW,
    )

    batch = await provider.get_security_master(("600183.SH", "000333.SZ"))

    assert batch.completeness is DataCompleteness.COMPLETE
    assert tuple(record.symbol for record in batch.records) == (
        "000333.SZ",
        "600183.SH",
    )
    assert tuple(record.exchange for record in batch.records) == (
        SecurityExchange.XSHE,
        SecurityExchange.XSHG,
    )
    assert all(record.source == TUSHARE_SOURCE for record in batch.records)
    assert [api_name for api_name, _ in client.calls] == [
        "stock_basic",
        "stock_basic",
    ]
    assert {cast(str, kwargs["ts_code"]) for _, kwargs in client.calls} == {
        "000333.SZ",
        "600183.SH",
    }


@pytest.mark.asyncio
async def test_index_basic_does_not_invent_currency_or_list_status() -> None:
    client = FakeClient({"index_basic": FakeTable([index_basic_row()])})
    provider = TushareSecurityMasterProvider(
        client,
        {"000001.SH": SecurityCategory.INDEX},
        now=lambda: FIXED_NOW,
    )

    batch = await provider.get_security_master(("000001.SH",))

    record = batch.records[0]
    assert record.security_type is SecurityCategory.INDEX
    assert record.exchange is SecurityExchange.XSHG
    assert record.currency is None
    assert record.list_status is None
    assert record.list_date == date(1991, 7, 15)
    assert client.calls[0][0] == "index_basic"


@pytest.mark.asyncio
async def test_stock_daily_converts_lots_and_thousand_cny_exactly() -> None:
    client = FakeClient({"daily": FakeTable([daily_row("600183.SH")])})
    provider = TushareDailyBarProvider(
        client,
        {"600183.SH": SecurityCategory.STOCK},
        now=lambda: FIXED_NOW,
    )

    batch = await provider.get_daily_bars(
        ("600183.SH",),
        TRADE_DATE,
        TRADE_DATE,
    )

    bar = batch.bars[0]
    assert bar.previous_close == Decimal("10.00")
    assert bar.close == Decimal("10.50")
    assert bar.volume == 1_234
    assert bar.turnover == Decimal("56789.000")
    assert bar.volume_unit is VolumeUnit.SHARE
    assert bar.turnover_unit is TurnoverUnit.CNY
    assert bar.adjustment is AdjustmentMode.NONE
    assert bar.trade_date == TRADE_DATE
    assert not hasattr(bar, "source_time")
    assert client.calls == [
        (
            "daily",
            {
                "ts_code": "600183.SH",
                "start_date": "20260724",
                "end_date": "20260724",
                "fields": (
                    "ts_code,trade_date,open,high,low,close,"
                    "pre_close,vol,amount"
                ),
            },
        )
    ]


@pytest.mark.asyncio
async def test_index_daily_uses_only_index_daily_api() -> None:
    client = FakeClient(
        {"index_daily": FakeTable([daily_row("000001.SH", volume="10")])}
    )
    provider = TushareDailyBarProvider(
        client,
        {"000001.SH": SecurityCategory.INDEX},
        now=lambda: FIXED_NOW,
    )

    batch = await provider.get_daily_bars(
        ("000001.SH",),
        TRADE_DATE,
        TRADE_DATE,
    )

    assert batch.completeness is DataCompleteness.COMPLETE
    assert batch.bars[0].volume == 1_000
    assert batch.bars[0].turnover == Decimal("56789.000")
    assert [api_name for api_name, _ in client.calls] == ["index_daily"]


@pytest.mark.asyncio
async def test_etf_is_explicitly_unsupported_without_any_api_call() -> None:
    client = FakeClient({})
    master_provider = TushareSecurityMasterProvider(
        client,
        {"510300.SH": SecurityCategory.ETF},
        now=lambda: FIXED_NOW,
    )
    daily_provider = TushareDailyBarProvider(
        client,
        {"510300.SH": SecurityCategory.ETF},
        now=lambda: FIXED_NOW,
    )

    master = await master_provider.get_security_master(("510300.SH",))
    daily = await daily_provider.get_daily_bars(
        ("510300.SH",),
        TRADE_DATE,
        TRADE_DATE,
    )

    assert master.completeness is DataCompleteness.FAILED
    assert daily.completeness is DataCompleteness.FAILED
    assert master.unsupported_symbols == ("510300.SH",)
    assert daily.unsupported_symbols == ("510300.SH",)
    assert client.calls == []


@pytest.mark.parametrize(
    ("error", "category", "code"),
    [
        (
            RuntimeError("抱歉，您没有接口(daily)访问权限"),
            MarketDataErrorCategory.AUTHORIZATION,
            "permission_denied",
        ),
        (
            RuntimeError("每分钟最多访问该接口1次"),
            MarketDataErrorCategory.RATE_LIMIT,
            "rate_limited",
        ),
        (
            RuntimeError("抱歉，您输入的TOKEN无效！"),
            MarketDataErrorCategory.AUTHORIZATION,
            "authentication_failed",
        ),
        (
            TimeoutError("request timed out"),
            MarketDataErrorCategory.TIMEOUT,
            "timeout",
        ),
    ],
)
@pytest.mark.asyncio
async def test_provider_errors_are_structurally_classified(
    error: BaseException,
    category: MarketDataErrorCategory,
    code: str,
) -> None:
    client = FakeClient({"daily": error})
    provider = TushareDailyBarProvider(
        client,
        {"600183.SH": SecurityCategory.STOCK},
        token=SecretStr(TOKEN),
        now=lambda: FIXED_NOW,
    )

    batch = await provider.get_daily_bars(
        ("600183.SH",),
        TRADE_DATE,
        TRADE_DATE,
    )

    assert batch.completeness is DataCompleteness.FAILED
    assert batch.provider_errors[0].category is category
    assert batch.provider_errors[0].code == code


@pytest.mark.asyncio
async def test_empty_dataframe_is_reported_as_missing() -> None:
    client = FakeClient(
        {
            "daily": FakeTable(
                [],
                columns=(
                    "ts_code",
                    "trade_date",
                    "open",
                    "high",
                    "low",
                    "close",
                    "pre_close",
                    "vol",
                    "amount",
                ),
            )
        }
    )
    provider = TushareDailyBarProvider(
        client,
        {"600183.SH": SecurityCategory.STOCK},
        now=lambda: FIXED_NOW,
    )

    batch = await provider.get_daily_bars(
        ("600183.SH",),
        TRADE_DATE,
        TRADE_DATE,
    )

    assert batch.completeness is DataCompleteness.FAILED
    assert batch.missing_symbols == ("600183.SH",)
    assert batch.provider_errors == ()


@pytest.mark.asyncio
async def test_one_empty_response_degrades_batch_to_partial() -> None:
    def response(**kwargs: object) -> FakeTable:
        symbol = cast(str, kwargs["ts_code"])
        return (
            FakeTable([daily_row(symbol)])
            if symbol == "600183.SH"
            else FakeTable([])
        )

    client = FakeClient({"daily": response})
    provider = TushareDailyBarProvider(
        client,
        {
            "600183.SH": SecurityCategory.STOCK,
            "000333.SZ": SecurityCategory.STOCK,
        },
        now=lambda: FIXED_NOW,
    )

    batch = await provider.get_daily_bars(
        ("600183.SH", "000333.SZ"),
        TRADE_DATE,
        TRADE_DATE,
    )

    assert batch.completeness is DataCompleteness.PARTIAL
    assert tuple(bar.symbol for bar in batch.bars) == ("600183.SH",)
    assert batch.missing_symbols == ("000333.SZ",)


@pytest.mark.asyncio
async def test_missing_required_field_is_a_protocol_error() -> None:
    row = daily_row("600183.SH")
    row.pop("amount")
    client = FakeClient({"daily": FakeTable([row])})
    provider = TushareDailyBarProvider(
        client,
        {"600183.SH": SecurityCategory.STOCK},
        now=lambda: FIXED_NOW,
    )

    batch = await provider.get_daily_bars(
        ("600183.SH",),
        TRADE_DATE,
        TRADE_DATE,
    )

    assert batch.completeness is DataCompleteness.FAILED
    assert batch.invalid_symbols == ("600183.SH",)
    assert batch.provider_errors[0].category is MarketDataErrorCategory.PROTOCOL
    assert batch.provider_errors[0].code == "invalid_response"


@pytest.mark.asyncio
async def test_inexact_lot_conversion_is_rejected_as_quality_error() -> None:
    client = FakeClient(
        {"daily": FakeTable([daily_row("600183.SH", volume="0.001")])}
    )
    provider = TushareDailyBarProvider(
        client,
        {"600183.SH": SecurityCategory.STOCK},
        now=lambda: FIXED_NOW,
    )

    batch = await provider.get_daily_bars(
        ("600183.SH",),
        TRADE_DATE,
        TRADE_DATE,
    )

    assert batch.invalid_symbols == ("600183.SH",)
    assert batch.provider_errors[0].category is MarketDataErrorCategory.QUALITY


@pytest.mark.asyncio
async def test_token_and_phone_are_removed_from_provider_errors() -> None:
    error = RuntimeError(f"token={TOKEN} phone=13800138000 unexpected failure")
    client = FakeClient({"daily": error})
    provider = TushareDailyBarProvider(
        client,
        {"600183.SH": SecurityCategory.STOCK},
        token=SecretStr(TOKEN),
        now=lambda: FIXED_NOW,
    )

    batch = await provider.get_daily_bars(
        ("600183.SH",),
        TRADE_DATE,
        TRADE_DATE,
    )
    serialized = json.dumps(batch.model_dump(mode="json"))

    assert TOKEN not in serialized
    assert "13800138000" not in serialized
    assert "[redacted-secret]" in serialized
    assert "[redacted-phone]" in serialized


def test_factory_reads_secretstr_and_never_calls_set_token() -> None:
    client = FakeClient({})
    module = FakeTushareModule(client)
    settings = Settings(
        _env_file=None,
        app_env="test",
        tushare_token=SecretStr(TOKEN),
    )  # type: ignore[call-arg]

    master, daily = build_tushare_reference_providers(
        settings,
        {"600183.SH": SecurityCategory.STOCK},
        now=lambda: FIXED_NOW,
        module_loader=lambda name: module,
    )

    assert isinstance(master, TushareSecurityMasterProvider)
    assert isinstance(daily, TushareDailyBarProvider)
    assert module.pro_api_calls == [TOKEN]
    assert TOKEN not in repr(master)
    assert TOKEN not in repr(daily)


def test_factory_failure_does_not_expose_token_in_exception() -> None:
    class FailingModule:
        @staticmethod
        def pro_api(token: str) -> object:
            raise RuntimeError(f"token={token} authentication failed")

    settings = Settings(
        _env_file=None,
        app_env="test",
        tushare_token=SecretStr(TOKEN),
    )  # type: ignore[call-arg]

    with pytest.raises(MarketDataProviderError) as exc_info:
        build_tushare_reference_providers(
            settings,
            {"600183.SH": SecurityCategory.STOCK},
            module_loader=lambda name: FailingModule(),
        )

    assert TOKEN not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
