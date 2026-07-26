from __future__ import annotations

import asyncio
import importlib
import math
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from pydantic import SecretStr, ValidationError

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
    ProviderError,
    SecurityCategory,
    SecurityExchange,
    SecurityMasterBatch,
    SecurityMasterRecord,
    TurnoverUnit,
    VolumeUnit,
)
from market_sentinel.market_data.errors import (
    MarketDataAuthorizationError,
    MarketDataProtocolError,
    MarketDataProviderError,
    MarketDataQualityError,
    MarketDataRateLimitError,
    MarketDataTimeoutError,
)
from market_sentinel.market_data.reference import DailyBarProvider, SecurityMasterProvider

TUSHARE_SOURCE = "tushare_pro"
_STOCK_BASIC_FIELDS = (
    "ts_code,symbol,name,market,exchange,curr_type,list_status,list_date"
)
_INDEX_BASIC_FIELDS = (
    "ts_code,name,fullname,market,publisher,index_type,category,list_date"
)
_DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,pre_close,vol,amount"
_LOT_SIZE = Decimal(100)
_THOUSAND = Decimal(1000)

_PHONE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_LABELED_SECRET_PATTERN = re.compile(
    r"(?i)\b(token|api[_ -]?key|authorization|cookie|password)\b"
    r"(\s*[:=]\s*|\s+)([^\s,;]+)"
)
_LONG_CREDENTIAL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z0-9_-]{20,}(?![A-Za-z0-9])"
)


class _ResponseProtocolError(ValueError):
    pass


class _ResponseQualityError(ValueError):
    pass


class _TushareReferenceProvider:
    def __init__(
        self,
        client: object,
        security_types: Mapping[str, SecurityCategory],
        *,
        token: SecretStr | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._security_types = dict(security_types)
        self._token = token
        self._now = now

    def _requested_symbols(self, symbols: Sequence[str]) -> tuple[str, ...]:
        requested = tuple(sorted(set(symbols)))
        if not requested:
            raise MarketDataQualityError("at least one canonical symbol is required")
        return requested

    async def _fetch_rows(
        self,
        api_name: str,
        symbol: str,
        request_kwargs: Mapping[str, str],
    ) -> tuple[list[Mapping[str, object]], ProviderError | None]:
        try:
            api = cast(Callable[..., object], getattr(self._client, api_name))
            table = await asyncio.to_thread(api, **dict(request_kwargs))
        except Exception as error:  # noqa: BLE001 - provider errors become structured metadata
            return [], self._provider_error(error, symbol)

        try:
            return _records_from_table(table), None
        except _ResponseProtocolError as error:
            return [], ProviderError(
                category=MarketDataErrorCategory.PROTOCOL,
                code="invalid_response",
                message=_sanitize_error(error, self._token),
                symbol=symbol,
            )

    def _provider_error(self, error: BaseException, symbol: str) -> ProviderError:
        category, code = _classify_error(error)
        return ProviderError(
            category=category,
            code=code,
            message=_sanitize_error(error, self._token),
            symbol=symbol,
        )

    @staticmethod
    def _security_type_error(symbol: str) -> ProviderError:
        return ProviderError(
            category=MarketDataErrorCategory.QUALITY,
            code="security_type_missing",
            message="requested symbol has no validated security type",
            symbol=symbol,
        )


class TushareSecurityMasterProvider(
    _TushareReferenceProvider,
    SecurityMasterProvider,
):
    async def get_security_master(self, symbols: Sequence[str]) -> SecurityMasterBatch:
        requested_at = self._now()
        requested = self._requested_symbols(symbols)
        records: list[SecurityMasterRecord] = []
        missing: list[str] = []
        unsupported: list[str] = []
        invalid: list[str] = []
        errors: list[ProviderError] = []

        for symbol in requested:
            security_type = self._security_types.get(symbol)
            if security_type is None:
                invalid.append(symbol)
                errors.append(self._security_type_error(symbol))
                continue
            if security_type is SecurityCategory.ETF:
                unsupported.append(symbol)
                continue

            api_name = (
                "stock_basic"
                if security_type is SecurityCategory.STOCK
                else "index_basic"
            )
            fields = (
                _STOCK_BASIC_FIELDS
                if security_type is SecurityCategory.STOCK
                else _INDEX_BASIC_FIELDS
            )
            rows, provider_error = await self._fetch_rows(
                api_name,
                symbol,
                {"ts_code": symbol, "fields": fields},
            )
            if provider_error is not None:
                errors.append(provider_error)
                continue
            if not rows:
                missing.append(symbol)
                continue

            try:
                record = _convert_security_master_row(
                    rows,
                    requested_symbol=symbol,
                    security_type=security_type,
                    received_at=self._now(),
                )
            except _ResponseProtocolError as error:
                invalid.append(symbol)
                errors.append(
                    ProviderError(
                        category=MarketDataErrorCategory.PROTOCOL,
                        code="invalid_response",
                        message=_sanitize_error(error, self._token),
                        symbol=symbol,
                    )
                )
            except (_ResponseQualityError, ValidationError) as error:
                invalid.append(symbol)
                errors.append(
                    ProviderError(
                        category=MarketDataErrorCategory.QUALITY,
                        code="invalid_record",
                        message=_sanitize_error(error, self._token),
                        symbol=symbol,
                    )
                )
            else:
                records.append(record)

        completed_at = self._now()
        completeness = _completeness(
            requested_count=len(requested),
            returned_count=len(records),
            has_issues=bool(missing or unsupported or invalid or errors),
        )
        return SecurityMasterBatch(
            requested_symbols=requested,
            records=tuple(records),
            missing_symbols=tuple(missing),
            unsupported_symbols=tuple(unsupported),
            invalid_symbols=tuple(invalid),
            provider_errors=tuple(errors),
            completeness=completeness,
            source=TUSHARE_SOURCE,
            requested_at=requested_at,
            completed_at=completed_at,
        )


class TushareDailyBarProvider(
    _TushareReferenceProvider,
    DailyBarProvider,
):
    async def get_daily_bars(
        self,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> DailyBarBatch:
        if end_date < start_date:
            raise MarketDataQualityError("end_date must not be earlier than start_date")

        requested_at = self._now()
        requested = self._requested_symbols(symbols)
        bars: list[DailyBar] = []
        missing: list[str] = []
        unsupported: list[str] = []
        invalid: list[str] = []
        errors: list[ProviderError] = []

        for symbol in requested:
            security_type = self._security_types.get(symbol)
            if security_type is None:
                invalid.append(symbol)
                errors.append(self._security_type_error(symbol))
                continue
            if security_type is SecurityCategory.ETF:
                unsupported.append(symbol)
                continue

            api_name = "daily" if security_type is SecurityCategory.STOCK else "index_daily"
            rows, provider_error = await self._fetch_rows(
                api_name,
                symbol,
                {
                    "ts_code": symbol,
                    "start_date": start_date.strftime("%Y%m%d"),
                    "end_date": end_date.strftime("%Y%m%d"),
                    "fields": _DAILY_FIELDS,
                },
            )
            if provider_error is not None:
                errors.append(provider_error)
                continue
            if not rows:
                missing.append(symbol)
                continue

            try:
                converted = _convert_daily_rows(
                    rows,
                    requested_symbol=symbol,
                    start_date=start_date,
                    end_date=end_date,
                    received_at=self._now(),
                )
            except _ResponseProtocolError as error:
                invalid.append(symbol)
                errors.append(
                    ProviderError(
                        category=MarketDataErrorCategory.PROTOCOL,
                        code="invalid_response",
                        message=_sanitize_error(error, self._token),
                        symbol=symbol,
                    )
                )
            except (_ResponseQualityError, ValidationError) as error:
                invalid.append(symbol)
                errors.append(
                    ProviderError(
                        category=MarketDataErrorCategory.QUALITY,
                        code="invalid_record",
                        message=_sanitize_error(error, self._token),
                        symbol=symbol,
                    )
                )
            else:
                bars.extend(converted)

        completed_at = self._now()
        returned_symbols = {bar.symbol for bar in bars}
        completeness = _completeness(
            requested_count=len(requested),
            returned_count=len(returned_symbols),
            has_issues=bool(missing or unsupported or invalid or errors),
        )
        return DailyBarBatch(
            requested_symbols=requested,
            bars=tuple(bars),
            missing_symbols=tuple(missing),
            unsupported_symbols=tuple(unsupported),
            invalid_symbols=tuple(invalid),
            provider_errors=tuple(errors),
            completeness=completeness,
            source=TUSHARE_SOURCE,
            requested_at=requested_at,
            completed_at=completed_at,
        )


def build_tushare_reference_providers(
    settings: Settings,
    security_types: Mapping[str, SecurityCategory],
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    module_loader: Callable[[str], object] = importlib.import_module,
) -> tuple[TushareSecurityMasterProvider, TushareDailyBarProvider]:
    token = settings.tushare_token
    if token is None or not token.get_secret_value():
        raise MarketDataAuthorizationError("TUSHARE_TOKEN is required")

    try:
        tushare_module = module_loader("tushare")
        pro_api = cast(Callable[[str], object], cast(Any, tushare_module).pro_api)
        client = pro_api(token.get_secret_value())
    except Exception as error:  # noqa: BLE001 - SDK startup errors must be sanitized
        safe_message = _sanitize_error(error, token)
        category, _ = _classify_error(error)
        exception_type = _exception_type(category)
        raise exception_type(safe_message) from None

    return (
        TushareSecurityMasterProvider(
            client,
            security_types,
            token=token,
            now=now,
        ),
        TushareDailyBarProvider(
            client,
            security_types,
            token=token,
            now=now,
        ),
    )


def _records_from_table(table: object) -> list[Mapping[str, object]]:
    if not hasattr(table, "columns") or not hasattr(table, "to_dict"):
        raise _ResponseProtocolError("Tushare response is not a DataFrame-like table")
    try:
        records = cast(Any, table).to_dict(orient="records")
    except Exception as error:
        raise _ResponseProtocolError("Tushare response cannot be converted to records") from error
    if not isinstance(records, list):
        raise _ResponseProtocolError("Tushare response records must be a list")
    if not all(isinstance(record, Mapping) for record in records):
        raise _ResponseProtocolError("Tushare response contains a non-mapping row")
    return [cast(Mapping[str, object], record) for record in records]


def _convert_security_master_row(
    rows: Sequence[Mapping[str, object]],
    *,
    requested_symbol: str,
    security_type: SecurityCategory,
    received_at: datetime,
) -> SecurityMasterRecord:
    matching = [row for row in rows if _required_text(row, "ts_code") == requested_symbol]
    if len(matching) != 1:
        raise _ResponseProtocolError(
            "security master response must contain exactly one requested symbol row"
        )
    row = matching[0]
    name = _required_text(row, "name")
    list_date = _optional_date(row.get("list_date"))
    exchange = _exchange_from_symbol(requested_symbol)

    if security_type is SecurityCategory.STOCK:
        provider_exchange = _required_text(row, "exchange")
        expected_provider_exchange = (
            "SSE" if exchange is SecurityExchange.XSHG else "SZSE"
        )
        if provider_exchange != expected_provider_exchange:
            raise _ResponseQualityError("provider exchange does not match symbol")
        if _required_text(row, "curr_type") != Currency.CNY.value:
            raise _ResponseQualityError("stock currency must be CNY")
        currency: Currency | None = Currency.CNY
        list_status: ListStatus | None = _list_status(
            _required_text(row, "list_status")
        )
    else:
        currency = None
        list_status = None

    return SecurityMasterRecord(
        symbol=requested_symbol,
        name=name,
        market=TradingMarket.A_SHARE,
        exchange=exchange,
        security_type=security_type,
        currency=currency,
        list_status=list_status,
        list_date=list_date,
        source=TUSHARE_SOURCE,
        provider_symbol=requested_symbol,
        received_at=received_at,
    )


def _convert_daily_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    requested_symbol: str,
    start_date: date,
    end_date: date,
    received_at: datetime,
) -> tuple[DailyBar, ...]:
    bars: list[DailyBar] = []
    seen_dates: set[date] = set()
    for row in rows:
        if _required_text(row, "ts_code") != requested_symbol:
            raise _ResponseProtocolError("daily response contains an unrequested symbol")
        trade_date = _required_date(row, "trade_date")
        if not start_date <= trade_date <= end_date:
            raise _ResponseProtocolError("daily response date is outside the requested range")
        if trade_date in seen_dates:
            raise _ResponseProtocolError("daily response contains a duplicate trade date")
        seen_dates.add(trade_date)

        bars.append(
            DailyBar(
                symbol=requested_symbol,
                trade_date=trade_date,
                source=TUSHARE_SOURCE,
                received_at=received_at,
                previous_close=_required_decimal(row, "pre_close"),
                open=_required_decimal(row, "open"),
                high=_required_decimal(row, "high"),
                low=_required_decimal(row, "low"),
                close=_required_decimal(row, "close"),
                volume=_lots_to_shares(_required_decimal(row, "vol")),
                turnover=_required_decimal(row, "amount") * _THOUSAND,
                volume_unit=VolumeUnit.SHARE,
                turnover_unit=TurnoverUnit.CNY,
                adjustment=AdjustmentMode.NONE,
            )
        )
    return tuple(bars)


def _required_text(row: Mapping[str, object], field: str) -> str:
    if field not in row or _is_missing(row[field]):
        raise _ResponseProtocolError(f"required field is missing: {field}")
    value = str(row[field]).strip()
    if not value:
        raise _ResponseProtocolError(f"required field is blank: {field}")
    return value


def _required_decimal(row: Mapping[str, object], field: str) -> Decimal:
    if field not in row or _is_missing(row[field]):
        raise _ResponseProtocolError(f"required field is missing: {field}")
    try:
        value = Decimal(str(row[field]))
    except (InvalidOperation, ValueError) as error:
        raise _ResponseQualityError(f"field is not a valid decimal: {field}") from error
    if not value.is_finite():
        raise _ResponseQualityError(f"field is not a finite decimal: {field}")
    return value


def _required_date(row: Mapping[str, object], field: str) -> date:
    if field not in row or _is_missing(row[field]):
        raise _ResponseProtocolError(f"required field is missing: {field}")
    try:
        return _parse_yyyymmdd(str(row[field]))
    except ValueError as error:
        raise _ResponseQualityError(f"field is not a valid YYYYMMDD date: {field}") from error


def _optional_date(value: object) -> date | None:
    if _is_missing(value):
        return None
    try:
        return _parse_yyyymmdd(str(value))
    except ValueError as error:
        raise _ResponseQualityError("list_date is not a valid YYYYMMDD date") from error


def _parse_yyyymmdd(value: str) -> date:
    if not re.fullmatch(r"\d{8}", value):
        raise ValueError("date must use YYYYMMDD")
    return date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:]}")


def _lots_to_shares(lots: Decimal) -> int:
    shares = lots * _LOT_SIZE
    if shares < 0:
        raise _ResponseQualityError("volume must not be negative")
    if shares != shares.to_integral_value():
        raise _ResponseQualityError("lot volume cannot be converted to an integer share count")
    return int(shares)


def _exchange_from_symbol(symbol: str) -> SecurityExchange:
    if symbol.endswith(".SH"):
        return SecurityExchange.XSHG
    if symbol.endswith(".SZ"):
        return SecurityExchange.XSHE
    raise _ResponseQualityError("symbol must use the canonical .SH or .SZ suffix")


def _list_status(value: str) -> ListStatus:
    statuses = {
        "L": ListStatus.LISTED,
        "P": ListStatus.PAUSED,
        "D": ListStatus.DELISTED,
    }
    try:
        return statuses[value]
    except KeyError as error:
        raise _ResponseQualityError("unsupported stock list_status") from error


def _is_missing(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, float):
        return math.isnan(value)
    try:
        unequal = cast(Any, value) != cast(Any, value)
        return isinstance(unequal, bool) and unequal
    except (TypeError, ValueError):
        return False


def _completeness(
    *,
    requested_count: int,
    returned_count: int,
    has_issues: bool,
) -> DataCompleteness:
    if returned_count == requested_count and not has_issues:
        return DataCompleteness.COMPLETE
    if returned_count:
        return DataCompleteness.PARTIAL
    return DataCompleteness.FAILED


def _classify_error(
    error: BaseException,
) -> tuple[MarketDataErrorCategory, str]:
    text = f"{error.__class__.__name__} {error}".casefold()
    authentication_markers = (
        "token无效",
        "token 无效",
        "token失效",
        "token 失效",
        "token过期",
        "token 过期",
        "invalid token",
        "expired token",
        "authentication failed",
        "unauthorized",
        "认证失败",
        "鉴权失败",
    )
    permission_markers = (
        "没有访问该接口的权限",
        "无权限",
        "权限不足",
        "积分不足",
        "permission denied",
        "forbidden",
    )
    rate_limit_markers = (
        "每分钟最多",
        "访问频次",
        "频率限制",
        "rate limit",
        "too many requests",
        "429",
    )

    if isinstance(error, TimeoutError) or "timeout" in text or "timed out" in text:
        return MarketDataErrorCategory.TIMEOUT, "timeout"
    if any(marker in text for marker in authentication_markers):
        return MarketDataErrorCategory.AUTHORIZATION, "authentication_failed"
    if ("没有接口" in text and "访问权限" in text) or any(
        marker in text for marker in permission_markers
    ):
        return MarketDataErrorCategory.AUTHORIZATION, "permission_denied"
    if any(marker in text for marker in rate_limit_markers):
        return MarketDataErrorCategory.RATE_LIMIT, "rate_limited"
    return MarketDataErrorCategory.PROVIDER, "provider_error"


def _sanitize_error(error: BaseException, token: SecretStr | None) -> str:
    message = str(error)
    if token is not None:
        secret = token.get_secret_value()
        if secret:
            message = message.replace(secret, "[redacted-secret]")
    message = _LABELED_SECRET_PATTERN.sub(r"\1=[redacted-secret]", message)
    message = _PHONE_PATTERN.sub("[redacted-phone]", message)
    message = _EMAIL_PATTERN.sub("[redacted-email]", message)
    message = _LONG_CREDENTIAL_PATTERN.sub("[redacted-secret]", message)
    sanitized = " ".join(message.split())
    return (sanitized or error.__class__.__name__)[:500]


def _exception_type(
    category: MarketDataErrorCategory,
) -> type[MarketDataProviderError]:
    exception_types: dict[
        MarketDataErrorCategory,
        type[MarketDataProviderError],
    ] = {
        MarketDataErrorCategory.AUTHORIZATION: MarketDataAuthorizationError,
        MarketDataErrorCategory.RATE_LIMIT: MarketDataRateLimitError,
        MarketDataErrorCategory.TIMEOUT: MarketDataTimeoutError,
        MarketDataErrorCategory.PROTOCOL: MarketDataProtocolError,
        MarketDataErrorCategory.QUALITY: MarketDataQualityError,
    }
    return exception_types.get(category, MarketDataProviderError)
