from __future__ import annotations

import asyncio
import importlib
import math
import re
import socket
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol
from zoneinfo import ZoneInfo

from pydantic import ValidationError

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
from market_sentinel.market_data.errors import MarketDataQualityError

OPEND_SOURCE = "futu_opend"
DEFAULT_OPEND_HOST = "127.0.0.1"
DEFAULT_OPEND_PORT = 11111
DEFAULT_CONNECT_TIMEOUT_SECONDS = 2.0
MAX_SNAPSHOT_SYMBOLS = 400
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
CRITICAL_HOLDING_SYMBOLS = frozenset(
    {"510300.SH", "588200.SH", "600183.SH"}
)

_INTERNAL_SYMBOL = re.compile(r"^(?P<code>\d{6})\.(?P<exchange>SH|SZ)$")
_OPEND_SYMBOL = re.compile(r"^(?P<exchange>SH|SZ)\.(?P<code>\d{6})$")
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_SECRET = re.compile(
    r"(?i)\b(token|api[_ -]?key|authorization|cookie|password|passwd)\b"
    r"(\s*[:=]\s*|\s+)([^\s,;]+)"
)
_ACCOUNT = re.compile(
    r"(?i)(?<!\w)(account(?:[_ -]?(?:id|name))?|"
    r"user(?:[_ -]?(?:id|name))?|账号|账户|用户(?:id)?)"
    r"(\s*[:=：]\s*|\s+)([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SDK_OBJECT = re.compile(r"<[^>\r\n]*\bobject at 0x[0-9a-fA-F]+>")
_MEMORY_ADDRESS = re.compile(r"\b0x[0-9a-fA-F]{6,}\b")
_LONG_CREDENTIAL = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z0-9_-]{20,}(?![A-Za-z0-9])"
)


class OpenDQuoteClient(Protocol):
    """Minimal quote-only SDK surface; no trading context is accepted."""

    def get_market_state(
        self, provider_symbols: Sequence[str]
    ) -> tuple[dict[str, object], ...]: ...

    def get_market_snapshot(
        self, provider_symbols: Sequence[str]
    ) -> tuple[dict[str, object], ...]: ...

    def close(self) -> None: ...


class ClosableSocket(Protocol):
    def close(self) -> None: ...


OpenDClientFactory = Callable[[str, int], OpenDQuoteClient]
EndpointChecker = Callable[[str, int, float], None]
SocketConnector = Callable[..., ClosableSocket]
Clock = Callable[[], datetime]


class _OpenDClassifiedError(Exception):
    def __init__(
        self,
        category: MarketDataErrorCategory,
        code: str,
        message: str,
    ) -> None:
        self.category = category
        self.code = code
        self.safe_message = sanitize_opend_error(message)
        super().__init__(self.safe_message)


class FutuOpenDQuoteClient:
    """SDK/DataFrame boundary used only after endpoint preflight succeeds."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        futu_module: object | None = None,
    ) -> None:
        module = futu_module or _load_futu_module()
        context_type = getattr(module, "OpenQuoteContext", None)
        if not callable(context_type):
            raise _OpenDClassifiedError(
                MarketDataErrorCategory.PROTOCOL,
                "missing_quote_context",
                "futu-api does not expose OpenQuoteContext",
            )
        self._ret_ok = getattr(module, "RET_OK", 0)
        try:
            self._context = context_type(host=host, port=port)
        except Exception as error:
            raise classify_opend_error(error, operation="connect") from error

    def get_market_state(
        self, provider_symbols: Sequence[str]
    ) -> tuple[dict[str, object], ...]:
        return self._request("get_market_state", provider_symbols)

    def get_market_snapshot(
        self, provider_symbols: Sequence[str]
    ) -> tuple[dict[str, object], ...]:
        return self._request("get_market_snapshot", provider_symbols)

    def close(self) -> None:
        try:
            self._context.close()
        except Exception as error:
            raise classify_opend_error(error, operation="close") from error

    def _request(
        self,
        operation: str,
        provider_symbols: Sequence[str],
    ) -> tuple[dict[str, object], ...]:
        method = getattr(self._context, operation, None)
        if not callable(method):
            raise _OpenDClassifiedError(
                MarketDataErrorCategory.PROTOCOL,
                "missing_sdk_method",
                f"OpenQuoteContext does not expose {operation}",
            )
        try:
            response = method(list(provider_symbols))
        except Exception as error:
            raise classify_opend_error(error, operation=operation) from error
        if not isinstance(response, tuple) or len(response) != 2:
            raise _OpenDClassifiedError(
                MarketDataErrorCategory.PROTOCOL,
                "invalid_response",
                f"{operation} response must be a two-item tuple",
            )
        return_code, payload = response
        if return_code != self._ret_ok:
            raise classify_opend_error(str(payload), operation=operation)
        return _records_from_table(payload, operation)


class OpenDMarketDataProvider(QuoteMarketDataProvider):
    """Read-only, single-batch A-share quote adapter for a local OpenD."""

    def __init__(
        self,
        security_types: Mapping[str, SecurityCategory],
        *,
        host: str = DEFAULT_OPEND_HOST,
        port: int = DEFAULT_OPEND_PORT,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        critical_symbols: Sequence[str] = tuple(CRITICAL_HOLDING_SYMBOLS),
        client_factory: OpenDClientFactory = FutuOpenDQuoteClient,
        endpoint_checker: EndpointChecker | None = None,
        now: Clock = lambda: datetime.now(UTC),
    ) -> None:
        if not host.strip():
            raise ValueError("OpenD host must not be blank")
        if not 1 <= port <= 65535:
            raise ValueError("OpenD port must be between 1 and 65535")
        if not 0 < connect_timeout_seconds <= 2:
            raise ValueError("OpenD connect timeout must be greater than 0 and at most 2 seconds")
        self._security_types = dict(security_types)
        self._host = host
        self._port = port
        self._connect_timeout_seconds = connect_timeout_seconds
        self._critical_symbols = frozenset(critical_symbols)
        self._client_factory = client_factory
        self._endpoint_checker = endpoint_checker or check_opend_endpoint
        self._now = now

    async def get_quotes(
        self,
        symbols: Sequence[str],
        phase: MarketPhase,
    ) -> QuoteBatch:
        requested = _validate_requested_symbols(symbols)
        if len(requested) > MAX_SNAPSHOT_SYMBOLS:
            raise MarketDataQualityError(
                f"OpenD single-batch snapshot limit is {MAX_SNAPSHOT_SYMBOLS} symbols"
            )
        return await asyncio.to_thread(self._get_quotes_sync, requested, phase)

    def _get_quotes_sync(
        self,
        requested: tuple[str, ...],
        phase: MarketPhase,
    ) -> QuoteBatch:
        requested_at = _utc_now(self._now)
        try:
            self._endpoint_checker(
                self._host,
                self._port,
                self._connect_timeout_seconds,
            )
        except Exception as error:  # noqa: BLE001 - endpoint checker is injectable.
            return self._failed_batch(
                requested,
                phase,
                requested_at,
                classify_opend_error(error, operation="endpoint_preflight"),
            )

        client: OpenDQuoteClient | None = None
        state_rows: tuple[dict[str, object], ...] = ()
        snapshot_rows: tuple[dict[str, object], ...] | None = None
        received_at: datetime | None = None
        errors: list[ProviderError] = []
        snapshot_calls = 0
        market_state_calls = 0
        provider_symbols = tuple(internal_to_opend_symbol(symbol) for symbol in requested)
        try:
            client = self._client_factory(self._host, self._port)
        except Exception as error:  # noqa: BLE001 - client factory is injectable.
            errors.append(_provider_error(classify_opend_error(error, operation="connect")))
        if client is not None:
            try:
                try:
                    market_state_calls += 1
                    state_rows = client.get_market_state(provider_symbols)
                except Exception as error:  # noqa: BLE001 - SDK errors are not stable.
                    errors.append(
                        _provider_error(
                            classify_opend_error(error, operation="get_market_state")
                        )
                    )
                try:
                    snapshot_calls += 1
                    snapshot_rows = client.get_market_snapshot(provider_symbols)
                except Exception as error:  # noqa: BLE001 - SDK errors are not stable.
                    errors.append(
                        _provider_error(
                            classify_opend_error(error, operation="get_market_snapshot")
                        )
                    )
                else:
                    received_at = _utc_now(self._now)
            finally:
                try:
                    client.close()
                except Exception as error:  # noqa: BLE001 - SDK errors are not stable.
                    errors.append(
                        _provider_error(classify_opend_error(error, operation="close"))
                    )

        if snapshot_rows is None:
            completed_at = _utc_now(self._now)
            return QuoteBatch(
                requested_symbols=requested,
                quotes=(),
                provider_errors=_deduplicate_errors(errors),
                returned_count=0,
                snapshot_calls=snapshot_calls,
                market_state_calls=market_state_calls,
                network_calls=snapshot_calls + market_state_calls,
                completeness=DataCompleteness.FAILED,
                coverage_ratio=Decimal(0),
                source=OPEND_SOURCE,
                market_phase=phase,
                market_state=QuoteMarketState.UNKNOWN,
                freshness=QuoteFreshness.UNKNOWN_MARKET_STATE,
                requested_at=requested_at,
                completed_at=completed_at,
            )

        if received_at is None:
            raise MarketDataQualityError(
                "received_at was not recorded for a successful OpenD snapshot"
            )
        return self._build_batch(
            requested=requested,
            phase=phase,
            requested_at=requested_at,
            received_at=received_at,
            state_rows=state_rows,
            snapshot_rows=snapshot_rows,
            errors=errors,
            snapshot_calls=snapshot_calls,
            market_state_calls=market_state_calls,
        )

    def _build_batch(
        self,
        *,
        requested: tuple[str, ...],
        phase: MarketPhase,
        requested_at: datetime,
        received_at: datetime,
        state_rows: Sequence[Mapping[str, object]],
        snapshot_rows: Sequence[Mapping[str, object]],
        errors: list[ProviderError],
        snapshot_calls: int,
        market_state_calls: int,
    ) -> QuoteBatch:
        requested_provider = {
            internal_to_opend_symbol(symbol): symbol for symbol in requested
        }
        market_states, raw_market_states = _market_states_by_provider_symbol(
            state_rows,
            requested_provider_symbols=set(requested_provider),
        )
        batch_market_state = _aggregate_market_state(
            tuple(market_states.get(symbol, QuoteMarketState.UNKNOWN) for symbol in requested_provider)
        )

        grouped: dict[str, list[Mapping[str, object]]] = {}
        unexpected: set[str] = set()
        for row in snapshot_rows:
            provider_symbol = str(row.get("code", "")).strip()
            if provider_symbol not in requested_provider:
                try:
                    unexpected.add(opend_to_internal_symbol(provider_symbol))
                except MarketDataQualityError as error:
                    errors.append(
                        ProviderError(
                            category=MarketDataErrorCategory.PROTOCOL,
                            code="invalid_unexpected_symbol",
                            message=sanitize_opend_error(str(error)),
                        )
                    )
                continue
            grouped.setdefault(provider_symbol, []).append(row)

        duplicate = {
            requested_provider[provider_symbol]
            for provider_symbol, rows in grouped.items()
            if len(rows) > 1
        }
        invalid: set[str] = set(duplicate)
        missing = {
            requested_provider[provider_symbol]
            for provider_symbol in set(requested_provider) - set(grouped)
        }
        issues: list[QualityIssue] = [
            QualityIssue(
                code="duplicate_symbol",
                severity=QualitySeverity.HIGH,
                message="OpenD returned duplicate rows for the requested symbol",
                symbol=symbol,
            )
            for symbol in duplicate
        ]
        quotes: list[MarketQuote] = []

        for provider_symbol in sorted(grouped):
            symbol = requested_provider[provider_symbol]
            if symbol in duplicate:
                continue
            security_type = self._security_types.get(symbol)
            if security_type is None:
                invalid.add(symbol)
                issues.append(
                    QualityIssue(
                        code="security_type_missing",
                        severity=QualitySeverity.HIGH,
                        message="validated security type is required before requesting quotes",
                        symbol=symbol,
                    )
                )
                continue
            try:
                quote = _convert_quote(
                    grouped[provider_symbol][0],
                    symbol=symbol,
                    security_type=security_type,
                    phase=phase,
                    market_state=market_states.get(
                        provider_symbol, QuoteMarketState.UNKNOWN
                    ),
                    received_at=received_at,
                )
            except (MarketDataQualityError, ValidationError) as error:
                invalid.add(symbol)
                code = _quality_error_code(error)
                issues.append(
                    QualityIssue(
                        code=code,
                        severity=QualitySeverity.HIGH,
                        message=sanitize_opend_error(str(error)),
                        symbol=symbol,
                    )
                )
                continue
            quotes.append(quote)
            if quote.trading_status is TradingStatus.SUSPENDED:
                issues.append(
                    QualityIssue(
                        code="suspended",
                        severity=QualitySeverity.WARNING,
                        message="OpenD reports that the security is suspended",
                        symbol=symbol,
                    )
                )

        unavailable = missing | invalid
        critical_missing = set(requested) & self._critical_symbols & unavailable
        for symbol in sorted(critical_missing):
            issues.append(
                QualityIssue(
                    code="critical_symbol_unavailable",
                    severity=QualitySeverity.CRITICAL,
                    message="critical holding has no usable quote",
                    symbol=symbol,
                )
            )

        completed_at = _utc_now(self._now)
        has_batch_issues = bool(missing or invalid or duplicate or unexpected or errors)
        completeness = (
            DataCompleteness.FAILED
            if not quotes
            else DataCompleteness.PARTIAL
            if has_batch_issues
            else DataCompleteness.COMPLETE
        )
        freshness = (
            QuoteFreshness.NOT_VERIFIED_CONTINUOUS_TRADING
            if batch_market_state is QuoteMarketState.CONTINUOUS_TRADING
            else QuoteFreshness.UNKNOWN_MARKET_STATE
            if batch_market_state is QuoteMarketState.UNKNOWN
            else QuoteFreshness.OUTSIDE_CONTINUOUS_TRADING
        )
        return QuoteBatch(
            requested_symbols=requested,
            quotes=tuple(quotes),
            missing_symbols=tuple(missing),
            invalid_symbols=tuple(invalid),
            duplicate_symbols=tuple(duplicate),
            unexpected_symbols=tuple(unexpected),
            critical_missing_symbols=tuple(critical_missing),
            provider_errors=_deduplicate_errors(errors),
            quality_issues=tuple(issues),
            returned_count=sum(
                provider_symbol in requested_provider
                for provider_symbol in (
                    str(row.get("code", "")).strip() for row in snapshot_rows
                )
            ),
            snapshot_calls=snapshot_calls,
            market_state_calls=market_state_calls,
            network_calls=snapshot_calls + market_state_calls,
            completeness=completeness,
            coverage_ratio=Decimal(len(quotes)) / Decimal(len(requested)),
            source=OPEND_SOURCE,
            market_phase=phase,
            market_state=batch_market_state,
            raw_market_states=tuple(raw_market_states.values()),
            freshness=freshness,
            requested_at=requested_at,
            completed_at=completed_at,
        )

    def _failed_batch(
        self,
        requested: tuple[str, ...],
        phase: MarketPhase,
        requested_at: datetime,
        error: _OpenDClassifiedError,
    ) -> QuoteBatch:
        return QuoteBatch(
            requested_symbols=requested,
            quotes=(),
            provider_errors=(_provider_error(error),),
            returned_count=0,
            completeness=DataCompleteness.FAILED,
            coverage_ratio=Decimal(0),
            source=OPEND_SOURCE,
            market_phase=phase,
            market_state=QuoteMarketState.UNKNOWN,
            freshness=QuoteFreshness.UNKNOWN_MARKET_STATE,
            requested_at=requested_at,
            completed_at=_utc_now(self._now),
        )


def build_opend_market_data_provider(
    settings: Settings,
    security_types: Mapping[str, SecurityCategory],
    *,
    critical_symbols: Sequence[str] = tuple(CRITICAL_HOLDING_SYMBOLS),
) -> OpenDMarketDataProvider:
    return OpenDMarketDataProvider(
        security_types,
        host=settings.opend_host,
        port=settings.opend_port,
        connect_timeout_seconds=settings.opend_connect_timeout_seconds,
        critical_symbols=critical_symbols,
    )


def check_opend_endpoint(
    host: str,
    port: int,
    timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    *,
    connector: SocketConnector = socket.create_connection,
) -> None:
    connection: ClosableSocket | None = None
    try:
        connection = connector((host, port), timeout=timeout_seconds)
    except ConnectionRefusedError as error:
        raise _OpenDClassifiedError(
            MarketDataErrorCategory.CONNECTION_REFUSED,
            "connection_refused",
            f"OpenD connection refused at {host}:{port}; start and log in to OpenD",
        ) from error
    except TimeoutError as error:
        raise _OpenDClassifiedError(
            MarketDataErrorCategory.OPEND_UNAVAILABLE,
            "opend_unavailable",
            f"OpenD endpoint timed out at {host}:{port}; start and log in to OpenD",
        ) from error
    except OSError as error:
        raise _OpenDClassifiedError(
            MarketDataErrorCategory.OPEND_UNAVAILABLE,
            "opend_unavailable",
            f"OpenD endpoint is unavailable at {host}:{port}; start and log in to OpenD",
        ) from error
    finally:
        if connection is not None:
            connection.close()


def internal_to_opend_symbol(symbol: str) -> str:
    match = _INTERNAL_SYMBOL.fullmatch(symbol)
    if match is None:
        raise MarketDataQualityError(
            "symbol must use canonical NNNNNN.SH or NNNNNN.SZ form"
        )
    return f"{match.group('exchange')}.{match.group('code')}"


def opend_to_internal_symbol(symbol: str) -> str:
    match = _OPEND_SYMBOL.fullmatch(symbol)
    if match is None:
        raise MarketDataQualityError(
            "OpenD symbol must use SH.NNNNNN or SZ.NNNNNN form"
        )
    return f"{match.group('code')}.{match.group('exchange')}"


def classify_opend_error(
    error: BaseException | str,
    *,
    operation: str,
) -> _OpenDClassifiedError:
    if isinstance(error, _OpenDClassifiedError):
        return error
    message = sanitize_opend_error(str(error))
    lowered = message.lower()
    if isinstance(error, ConnectionRefusedError) or any(
        phrase in lowered for phrase in ("connection refused", "econnrefused")
    ):
        category = MarketDataErrorCategory.CONNECTION_REFUSED
        code = "connection_refused"
    elif isinstance(error, TimeoutError) or any(
        phrase in lowered for phrase in ("timeout", "timed out", "超时")
    ):
        category = MarketDataErrorCategory.TIMEOUT
        code = "timeout"
    elif isinstance(error, (ConnectionError, OSError)) or any(
        phrase in lowered
        for phrase in ("cannot connect", "connect failed", "opend is not started", "无法连接")
    ):
        category = MarketDataErrorCategory.OPEND_UNAVAILABLE
        code = "opend_unavailable"
    elif any(
        phrase in lowered
        for phrase in (
            "quote login",
            "qot login",
            "login failed",
            "not logged in",
            "行情登录失败",
        )
    ):
        category = MarketDataErrorCategory.AUTHENTICATION_FAILED
        code = "authentication_failed"
    elif any(
        phrase in lowered
        for phrase in (
            "quote right",
            "permission denied",
            "no permission",
            "权限不足",
            "没有行情权限",
        )
    ):
        category = MarketDataErrorCategory.PERMISSION_DENIED
        code = "permission_denied"
    elif any(
        phrase in lowered
        for phrase in (
            "rate limit",
            "too many requests",
            "frequency limit",
            "频率超限",
            "次/分钟",
            "次/小时",
        )
    ):
        category = MarketDataErrorCategory.RATE_LIMIT
        code = "rate_limited"
    elif isinstance(error, str):
        category = MarketDataErrorCategory.PROTOCOL
        code = "protocol_error"
    elif isinstance(error, (TypeError, ValueError)):
        category = MarketDataErrorCategory.INVALID_RESPONSE
        code = "invalid_response"
    else:
        category = MarketDataErrorCategory.UNEXPECTED
        code = "unexpected_error"
    return _OpenDClassifiedError(category, code, f"{operation}: {message or code}")


def sanitize_opend_error(error: BaseException | str) -> str:
    message = str(error)
    message = "".join(
        character
        for character in message
        if not unicodedata.category(character).startswith("C")
    )
    message = _SDK_OBJECT.sub("[redacted-sdk-object]", message)
    message = _MEMORY_ADDRESS.sub("[redacted-memory-address]", message)
    message = _PHONE.sub("[redacted-phone]", message)
    message = _EMAIL.sub("[redacted-email]", message)
    message = _BEARER.sub("Bearer [redacted-secret]", message)
    message = _SECRET.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[redacted-secret]",
        message,
    )
    message = _ACCOUNT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[redacted-account]",
        message,
    )
    message = _LONG_CREDENTIAL.sub("[redacted-secret]", message)
    return " ".join(message.split())[:500]


def _load_futu_module() -> object:
    try:
        return importlib.import_module("futu")
    except ImportError as error:
        raise _OpenDClassifiedError(
            MarketDataErrorCategory.PROVIDER,
            "missing_optional_dependency",
            "futu-api optional dependency is not installed; install market-sentinel[opend]",
        ) from error


def _records_from_table(
    table: object,
    operation: str,
) -> tuple[dict[str, object], ...]:
    raw_records: object
    if isinstance(table, (list, tuple)):
        raw_records = table
    else:
        to_dict = getattr(table, "to_dict", None)
        if not callable(to_dict):
            raise _OpenDClassifiedError(
                MarketDataErrorCategory.INVALID_RESPONSE,
                "invalid_response",
                f"{operation} success payload is not a table",
            )
        try:
            raw_records = to_dict(orient="records")
        except Exception as error:
            raise classify_opend_error(error, operation=operation) from error
    if not isinstance(raw_records, (list, tuple)):
        raise _OpenDClassifiedError(
            MarketDataErrorCategory.INVALID_RESPONSE,
            "invalid_response",
            f"{operation} table conversion did not return records",
        )
    records: list[dict[str, object]] = []
    for record in raw_records:
        if not isinstance(record, Mapping):
            raise _OpenDClassifiedError(
                MarketDataErrorCategory.INVALID_RESPONSE,
                "invalid_response",
                f"{operation} table contains a non-object record",
            )
        records.append({str(key): value for key, value in record.items()})
    return tuple(records)


def _validate_requested_symbols(symbols: Sequence[str]) -> tuple[str, ...]:
    supplied = tuple(symbols)
    if not supplied:
        raise MarketDataQualityError("at least one symbol is required")
    if len(supplied) != len(set(supplied)):
        raise MarketDataQualityError("requested symbols must not contain duplicates")
    for symbol in supplied:
        internal_to_opend_symbol(symbol)
    return tuple(sorted(supplied))


def _market_states_by_provider_symbol(
    rows: Sequence[Mapping[str, object]],
    *,
    requested_provider_symbols: set[str],
) -> tuple[dict[str, QuoteMarketState], dict[str, str]]:
    states: dict[str, QuoteMarketState] = {}
    raw_states: dict[str, str] = {}
    for row in rows:
        provider_symbol = str(row.get("code", "")).strip()
        if provider_symbol not in requested_provider_symbols:
            continue
        raw_state = sanitize_opend_market_state(row.get("market_state"))
        states.setdefault(provider_symbol, _map_market_state(raw_state))
        if raw_state:
            raw_states.setdefault(provider_symbol, raw_state)
    return dict(sorted(states.items())), dict(sorted(raw_states.items()))


def _map_market_state(value: object) -> QuoteMarketState:
    text = sanitize_opend_market_state(value)
    if text in {"MORNING", "AFTERNOON"}:
        return QuoteMarketState.CONTINUOUS_TRADING
    if text in {
        "PRE_MARKET_BEGIN",
        "PRE_MARKET_END",
        "AUCTION",
        "OPENING",
        "WAITING_OPEN",
    }:
        return QuoteMarketState.AUCTION
    if text in {"REST", "MIDDAY_BREAK"}:
        return QuoteMarketState.MIDDAY_BREAK
    if text in {
        "CLOSED",
        "AFTER_HOURS_BEGIN",
        "AFTER_HOURS_END",
        "STIB_AFTER_HOURS_BEGIN",
        "STIB_AFTER_HOURS_END",
    }:
        return QuoteMarketState.CLOSED
    return QuoteMarketState.UNKNOWN


def sanitize_opend_market_state(value: object) -> str:
    text = "".join(
        character
        for character in str(value or "")
        if not unicodedata.category(character).startswith("C")
    )
    text = " ".join(text.split()).upper()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    if re.fullmatch(r"[A-Z][A-Z0-9_]*", text):
        return text
    return sanitize_opend_error(text).upper()


def _aggregate_market_state(
    states: Sequence[QuoteMarketState],
) -> QuoteMarketState:
    distinct = set(states)
    return next(iter(distinct)) if len(distinct) == 1 else QuoteMarketState.UNKNOWN


def _convert_quote(
    row: Mapping[str, object],
    *,
    symbol: str,
    security_type: SecurityCategory,
    phase: MarketPhase,
    market_state: QuoteMarketState,
    received_at: datetime,
) -> MarketQuote:
    provider_symbol = str(row.get("code", "")).strip()
    if opend_to_internal_symbol(provider_symbol) != symbol:
        raise MarketDataQualityError("provider symbol does not match requested symbol")
    source_time = _parse_source_time(row.get("update_time"))
    if source_time is None:
        raise MarketDataQualityError("source_time is missing or invalid")
    if source_time.astimezone(UTC) > received_at:
        raise MarketDataQualityError("source_time is in the future")

    trading_status = _map_trading_status(row, market_state)
    allow_missing_prices = trading_status in {
        TradingStatus.SUSPENDED,
        TradingStatus.HALTED,
        TradingStatus.NO_TRADES,
    }
    previous_close = _positive_decimal(row.get("prev_close_price"), "previous_close")
    open_price = _optional_price(row.get("open_price"), "open", allow_missing_prices)
    high = _optional_price(row.get("high_price"), "high", allow_missing_prices)
    low = _optional_price(row.get("low_price"), "low", allow_missing_prices)
    last = _optional_price(row.get("last_price"), "last", allow_missing_prices)
    if not allow_missing_prices and any(
        value is None for value in (open_price, high, low, last)
    ):
        raise MarketDataQualityError("normal quote is missing required price fields")

    return MarketQuote(
        symbol=symbol,
        provider_symbol=provider_symbol,
        exchange=(
            SecurityExchange.XSHG if symbol.endswith(".SH") else SecurityExchange.XSHE
        ),
        market=TradingMarket.A_SHARE,
        security_type=security_type,
        currency=Currency.CNY,
        source=OPEND_SOURCE,
        source_time=source_time,
        received_at=received_at,
        previous_close=previous_close,
        open=open_price,
        high=high,
        low=low,
        last=last,
        volume=_non_negative_integer(row.get("volume"), "volume"),
        turnover=_non_negative_decimal(row.get("turnover"), "turnover"),
        market_phase=phase,
        trading_status=trading_status,
    )


def _parse_source_time(value: object) -> datetime | None:
    if _is_missing(value):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TIMEZONE)
    return parsed


def _positive_decimal(value: object, field_name: str) -> Decimal:
    parsed = _decimal(value, field_name)
    if parsed <= 0:
        raise MarketDataQualityError(f"{field_name} must be greater than zero")
    return parsed


def _optional_price(
    value: object,
    field_name: str,
    allow_missing: bool,
) -> Decimal | None:
    if _is_missing(value):
        return None
    parsed = _decimal(value, field_name)
    if parsed == 0 and allow_missing:
        return None
    if parsed <= 0:
        raise MarketDataQualityError(f"{field_name} must be greater than zero")
    return parsed


def _non_negative_decimal(value: object, field_name: str) -> Decimal | None:
    if _is_missing(value):
        return None
    parsed = _decimal(value, field_name)
    if parsed < 0:
        raise MarketDataQualityError(f"{field_name} must not be negative")
    return parsed


def _non_negative_integer(value: object, field_name: str) -> int | None:
    parsed = _non_negative_decimal(value, field_name)
    if parsed is None:
        return None
    if parsed != parsed.to_integral_value():
        raise MarketDataQualityError(f"{field_name} must use integral share units")
    return int(parsed)


def _decimal(value: object, field_name: str) -> Decimal:
    if _is_missing(value):
        raise MarketDataQualityError(f"{field_name} is missing")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise MarketDataQualityError(f"{field_name} is not a decimal number") from error
    if not parsed.is_finite():
        raise MarketDataQualityError(f"{field_name} must be finite")
    return parsed


def _is_missing(value: object) -> bool:
    return value is None or (
        isinstance(value, float) and math.isnan(value)
    ) or (isinstance(value, str) and not value.strip())


def _map_trading_status(
    row: Mapping[str, object],
    market_state: QuoteMarketState,
) -> TradingStatus:
    suspension = row.get("suspension")
    if suspension is True or str(suspension).strip().lower() in {"true", "1"}:
        return TradingStatus.SUSPENDED
    security_status = str(row.get("sec_status", "")).strip().upper()
    if "SUSPEND" in security_status:
        return TradingStatus.SUSPENDED
    if any(marker in security_status for marker in ("HALT", "DELIST")):
        return TradingStatus.HALTED
    if market_state is QuoteMarketState.AUCTION:
        return TradingStatus.AUCTION
    if market_state is QuoteMarketState.CONTINUOUS_TRADING:
        return TradingStatus.TRADING
    if market_state in {QuoteMarketState.CLOSED, QuoteMarketState.MIDDAY_BREAK}:
        return TradingStatus.CLOSED
    return TradingStatus.UNKNOWN


def _quality_error_code(error: BaseException) -> str:
    message = str(error).lower()
    if "future" in message:
        return "future_source_time"
    if "source_time" in message:
        return "source_time_missing"
    if "volume" in message:
        return "invalid_volume"
    if "turnover" in message:
        return "invalid_turnover"
    if any(
        field in message
        for field in ("previous_close", "high", "low", "open", "last", "price")
    ):
        return "invalid_price_relationship"
    return "invalid_quote"


def _provider_error(error: _OpenDClassifiedError) -> ProviderError:
    return ProviderError(
        category=error.category,
        code=error.code,
        message=error.safe_message,
    )


def _deduplicate_errors(errors: Sequence[ProviderError]) -> tuple[ProviderError, ...]:
    unique = {
        (error.category, error.code, error.symbol, error.message): error
        for error in errors
    }
    return tuple(unique.values())


def _utc_now(clock: Clock) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise MarketDataQualityError("provider clock must return timezone-aware datetime")
    return value.astimezone(UTC)
