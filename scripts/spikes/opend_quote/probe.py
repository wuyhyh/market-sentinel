from __future__ import annotations

import argparse
import importlib
import json
import math
import re
import socket
import sys
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast
from zoneinfo import ZoneInfo

from market_sentinel.domain.watchlist import SecurityType
from market_sentinel.watchlist import WatchlistConfigurationError, WatchlistLoader

PROJECT_ROOT = Path(__file__).resolve().parents[3]
REPORT_RELATIVE_PATH = Path(
    "data/spikes/opend_quote/a-share-realtime-capabilities.json"
)
DEFAULT_WATCHLIST_PATH = Path("config/watchlist.yaml")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 11111
DEFAULT_CONNECT_TIMEOUT_SECONDS = 2.0
OPEND_START_RECOMMENDATION = "启动并登录OpenD后重试"
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
CONTINUOUS_TRADING_STATES = frozenset({"MORNING", "AFTERNOON"})
REQUIRED_SAMPLE_SYMBOLS = (
    "000333.SZ",
    "159949.SZ",
    "588200.SH",
    "600183.SH",
)
CRITICAL_HOLDING_SYMBOLS = (
    "510300.SH",
    "588200.SH",
    "600183.SH",
)
SNAPSHOT_REQUIRED_FIELDS = (
    "code",
    "update_time",
    "last_price",
    "prev_close_price",
    "open_price",
    "high_price",
    "low_price",
    "volume",
    "turnover",
)
MAX_ERROR_MESSAGE_LENGTH = 500

INTERNAL_SYMBOL_PATTERN = re.compile(r"^(?P<code>\d{6})\.(?P<market>SH|SZ)$")
PROVIDER_SYMBOL_PATTERN = re.compile(r"^(?P<market>SH|SZ)\.(?P<code>\d{6})$")
PHONE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
LABELED_SECRET_PATTERN = re.compile(
    r"(?i)\b(token|api[_ -]?key|authorization|cookie|password|passwd)\b"
    r"(\s*[:=]\s*|\s+)([^\s,;]+)"
)
LABELED_ACCOUNT_PATTERN = re.compile(
    r"(?i)(?<!\w)(account(?:[_ -]?(?:id|name))?|"
    r"user(?:[_ -]?(?:id|name))?|账号|账户|用户(?:id)?)"
    r"(\s*[:=：]\s*|\s+)([^\s,;]+)"
)
BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
LONG_CREDENTIAL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z0-9_-]{20,}(?![A-Za-z0-9])"
)


class ProbeStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class ErrorCategory(StrEnum):
    OPEND_UNAVAILABLE = "opend_unavailable"
    UNAVAILABLE = "opend_unavailable"
    CONNECTION_REFUSED = "connection_refused"
    AUTHENTICATION_FAILED = "authentication_failed"
    PERMISSION_DENIED = "permission_denied"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    INVALID_RESPONSE = "invalid_response"
    PROTOCOL_ERROR = "protocol_error"
    UNEXPECTED_ERROR = "unexpected_error"


class FreshnessAssessment(StrEnum):
    NOT_VERIFIED_OUTSIDE_CONTINUOUS_TRADING = (
        "not_verified_outside_continuous_trading"
    )
    NOT_VERIFIED_EXPECT_LIVE_NOT_REQUESTED = (
        "not_verified_expect_live_not_requested"
    )
    LIVE_CHECKS_PASSED_SINGLE_SNAPSHOT_ONLY = (
        "live_checks_passed_single_snapshot_only"
    )
    LIVE_CHECKS_FAILED = "live_checks_failed"


class ProbeConfigurationError(Exception):
    """A local configuration error whose message is safe to display."""


class OpenDProbeError(Exception):
    def __init__(
        self,
        category: ErrorCategory,
        operation: str,
        message: str,
    ) -> None:
        self.category = category
        self.operation = operation
        self.safe_message = sanitize_error_message(message)
        super().__init__(self.safe_message)


@dataclass(frozen=True)
class ProbeSecurity:
    symbol: str
    security_type: SecurityType

    @property
    def provider_symbol(self) -> str:
        return internal_to_provider_symbol(self.symbol)


class QuoteClient(Protocol):
    sdk_version: str

    def get_market_state(
        self, provider_symbols: Sequence[str]
    ) -> tuple[dict[str, object], ...]: ...

    def get_market_snapshot(
        self, provider_symbols: Sequence[str]
    ) -> tuple[dict[str, object], ...]: ...

    def close(self) -> None: ...


class ClosableSocket(Protocol):
    def close(self) -> None: ...


ClientFactory = Callable[[str, int], QuoteClient]
EndpointChecker = Callable[[str, int, float], None]
SocketConnector = Callable[..., ClosableSocket]
Clock = Callable[[], datetime]
MonotonicClock = Callable[[], float]


class OpenDQuoteClientAdapter:
    """The only boundary that is allowed to see an SDK DataFrame."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        *,
        futu_module: object | None = None,
    ) -> None:
        module = futu_module or _load_futu_module()
        context_type = getattr(module, "OpenQuoteContext", None)
        if not callable(context_type):
            raise OpenDProbeError(
                ErrorCategory.PROTOCOL_ERROR,
                "connect",
                "futu-api does not expose OpenQuoteContext",
            )
        self._ret_ok = getattr(module, "RET_OK", 0)
        self.sdk_version = str(getattr(module, "__version__", "unknown"))
        try:
            self._context = context_type(host=host, port=port)
        except Exception as error:
            raise classify_error(error, operation="connect") from error

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
            raise classify_error(error, operation="close") from error

    def _request(
        self,
        operation: str,
        provider_symbols: Sequence[str],
    ) -> tuple[dict[str, object], ...]:
        method = getattr(self._context, operation, None)
        if not callable(method):
            raise OpenDProbeError(
                ErrorCategory.PROTOCOL_ERROR,
                operation,
                f"OpenQuoteContext does not expose {operation}",
            )
        try:
            result = method(list(provider_symbols))
        except Exception as error:
            raise classify_error(error, operation=operation) from error
        if not isinstance(result, tuple) or len(result) != 2:
            raise OpenDProbeError(
                ErrorCategory.PROTOCOL_ERROR,
                operation,
                "OpenD response must be a two-item tuple",
            )
        return_code, payload = result
        if return_code != self._ret_ok:
            raise classify_error(str(payload), operation=operation)
        return _table_to_records(payload, operation=operation)


def check_opend_endpoint(
    host: str,
    port: int,
    timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    *,
    connector: SocketConnector = socket.create_connection,
) -> None:
    """Fail before importing or constructing the retrying Futu SDK context."""
    connection: ClosableSocket | None = None
    try:
        connection = connector((host, port), timeout=timeout_seconds)
    except ConnectionRefusedError as error:
        raise OpenDProbeError(
            ErrorCategory.CONNECTION_REFUSED,
            "endpoint_preflight",
            f"OpenD connection refused at {host}:{port}; "
            f"{OPEND_START_RECOMMENDATION}",
        ) from error
    except TimeoutError as error:
        raise OpenDProbeError(
            ErrorCategory.OPEND_UNAVAILABLE,
            "endpoint_preflight",
            f"OpenD endpoint timed out at {host}:{port}; "
            f"{OPEND_START_RECOMMENDATION}",
        ) from error
    except OSError as error:
        raise OpenDProbeError(
            ErrorCategory.OPEND_UNAVAILABLE,
            "endpoint_preflight",
            f"OpenD endpoint is unavailable at {host}:{port}; "
            f"{OPEND_START_RECOMMENDATION}",
        ) from error
    finally:
        if connection is not None:
            connection.close()


def internal_to_provider_symbol(symbol: str) -> str:
    match = INTERNAL_SYMBOL_PATTERN.fullmatch(symbol)
    if match is None:
        raise ProbeConfigurationError(
            "symbols must use the canonical NNNNNN.SH or NNNNNN.SZ form"
        )
    return f"{match.group('market')}.{match.group('code')}"


def provider_to_internal_symbol(symbol: str) -> str:
    match = PROVIDER_SYMBOL_PATTERN.fullmatch(symbol)
    if match is None:
        raise ProbeConfigurationError(
            "OpenD symbols must use the SH.NNNNNN or SZ.NNNNNN form"
        )
    return f"{match.group('code')}.{match.group('market')}"


def load_probe_securities(
    config_path: Path,
) -> tuple[ProbeSecurity, ...]:
    try:
        watchlist = WatchlistLoader().load(config_path)
    except WatchlistConfigurationError as error:
        raise ProbeConfigurationError(
            f"watchlist validation failed for {config_path.as_posix()}"
        ) from error

    securities = tuple(
        sorted(
            (
                ProbeSecurity(
                    symbol=security.symbol,
                    security_type=security.security_type,
                )
                for security in watchlist.securities
                if security.enabled
                and security.security_type in {SecurityType.STOCK, SecurityType.ETF}
            ),
            key=lambda security: security.symbol,
        )
    )
    if not securities:
        raise ProbeConfigurationError(
            "watchlist contains no enabled A-share stocks or ETFs"
        )
    symbols = {security.symbol for security in securities}
    missing_samples = sorted(set(REQUIRED_SAMPLE_SYMBOLS) - symbols)
    missing_holdings = sorted(set(CRITICAL_HOLDING_SYMBOLS) - symbols)
    if missing_samples or missing_holdings:
        missing = sorted(set(missing_samples) | set(missing_holdings))
        raise ProbeConfigurationError(
            "watchlist is missing required probe symbols: " + ", ".join(missing)
        )
    return securities


def build_dry_run(
    securities: Sequence[ProbeSecurity],
    *,
    host: str,
    port: int,
) -> dict[str, object]:
    counts = Counter(security.security_type.value for security in securities)
    return {
        "status": "dry_run",
        "requested_count": len(securities),
        "stock_count": counts[SecurityType.STOCK.value],
        "etf_count": counts[SecurityType.ETF.value],
        "planned_snapshot_calls": 1,
        "planned_market_state_calls": 1,
        "normalized_sample_symbols": {
            symbol: internal_to_provider_symbol(symbol)
            for symbol in REQUIRED_SAMPLE_SYMBOLS
        },
        "host": host,
        "port": port,
        "network_calls": 0,
    }


def run_probe(
    client: QuoteClient,
    securities: Sequence[ProbeSecurity],
    *,
    host: str,
    port: int,
    expect_live: bool,
    now: Clock = lambda: datetime.now(UTC),
    monotonic: MonotonicClock = time.perf_counter,
) -> dict[str, object]:
    started_at = _require_aware(now())
    request_started = monotonic()
    provider_symbols = tuple(
        security.provider_symbol for security in securities
    )
    security_by_provider = {
        security.provider_symbol: security for security in securities
    }
    errors: list[dict[str, str]] = []
    market_state_calls = 0
    snapshot_calls = 0

    market_state_calls += 1
    try:
        market_state_rows = client.get_market_state(provider_symbols)
        market_states = _extract_market_states(
            market_state_rows,
            requested_provider_symbols=set(provider_symbols),
        )
    except Exception as error:  # noqa: BLE001 - fake and SDK clients share no error base.
        classified = classify_error(error, operation="get_market_state")
        errors.append(_error_payload(classified))
        market_states = {}

    snapshot_calls += 1
    try:
        snapshot_rows = client.get_market_snapshot(provider_symbols)
    except Exception as error:  # noqa: BLE001 - fake and SDK clients share no error base.
        classified = classify_error(error, operation="get_market_snapshot")
        errors.append(_error_payload(classified))
        completed_at = _require_aware(now())
        return _failed_report(
            client=client,
            securities=securities,
            host=host,
            port=port,
            expect_live=expect_live,
            started_at=started_at,
            completed_at=completed_at,
            elapsed_ms=_elapsed_ms(request_started, monotonic()),
            market_state_calls=market_state_calls,
            snapshot_calls=snapshot_calls,
            market_states=market_states,
            errors=errors,
        )

    received_at = _require_aware(now())
    elapsed_ms = _elapsed_ms(request_started, monotonic())
    normalized = _normalize_snapshot_rows(
        snapshot_rows,
        security_by_provider=security_by_provider,
        market_states=market_states,
        received_at=received_at,
        expect_live=expect_live,
    )
    completed_at = _require_aware(now())
    live_result = _assess_live_freshness(
        expect_live=expect_live,
        records=cast(Sequence[Mapping[str, object]], normalized["records"]),
        requested_symbols=tuple(security.symbol for security in securities),
    )
    status = _probe_status(
        missing_symbols=cast(Sequence[str], normalized["missing_symbols"]),
        duplicate_symbols=cast(Sequence[str], normalized["duplicate_symbols"]),
        unexpected_symbols=cast(Sequence[str], normalized["unexpected_symbols"]),
        missing_required_fields=cast(
            Mapping[str, Sequence[str]], normalized["missing_required_fields"]
        ),
        errors=errors,
        expect_live=expect_live,
        live_freshness_verified=bool(live_result["live_freshness_verified"]),
    )
    counts = Counter(security.security_type.value for security in securities)
    distinct_states = sorted(set(market_states.values()))

    return {
        "schema_version": 1,
        "probe_name": "opend_a_share_realtime_capabilities",
        "provider": "futu_opend",
        "sdk_version": client.sdk_version,
        "python_version": sys.version.split()[0],
        "host": host,
        "port": port,
        "started_at": _isoformat_utc(started_at),
        "completed_at": _isoformat_utc(completed_at),
        "received_at": _isoformat_utc(received_at),
        "elapsed_ms": elapsed_ms,
        "status": status.value,
        "requested_count": len(securities),
        "stock_count": counts[SecurityType.STOCK.value],
        "etf_count": counts[SecurityType.ETF.value],
        "returned_count": len(cast(Sequence[object], normalized["records"])),
        "requested_symbols": sorted(
            security.symbol for security in securities
        ),
        "records": normalized["records"],
        "missing_symbols": normalized["missing_symbols"],
        "duplicate_symbols": normalized["duplicate_symbols"],
        "unexpected_symbols": normalized["unexpected_symbols"],
        "missing_required_fields": normalized["missing_required_fields"],
        "returned_fields": normalized["returned_fields"],
        "market_state": (
            distinct_states[0]
            if len(distinct_states) == 1
            else "MIXED"
            if distinct_states
            else "UNKNOWN"
        ),
        "market_states": _internal_market_states(market_states),
        "freshness_assessment": live_result["freshness_assessment"],
        "live_freshness_verified": live_result["live_freshness_verified"],
        "continuous_updates_verified": False,
        "required_sample_coverage": live_result["required_sample_coverage"],
        "critical_holding_coverage": live_result["critical_holding_coverage"],
        "expect_live": expect_live,
        "market_state_calls": market_state_calls,
        "snapshot_calls": snapshot_calls,
        "network_calls": market_state_calls + snapshot_calls,
        "errors": errors,
        "notes": [
            (
                "provider_update_time is an OpenD provider update time, not proven "
                "to be an original exchange timestamp"
            ),
            "a single snapshot never verifies continuous updates",
            "volume and turnover units require separate official and live validation",
        ],
    }


def _normalize_snapshot_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    security_by_provider: Mapping[str, ProbeSecurity],
    market_states: Mapping[str, str],
    received_at: datetime,
    expect_live: bool,
) -> dict[str, object]:
    grouped: dict[str, list[Mapping[str, object]]] = {}
    unexpected: set[str] = set()
    returned_fields: set[str] = set()
    for row in rows:
        returned_fields.update(str(field) for field in row)
        provider_symbol = str(row.get("code", ""))
        if provider_symbol not in security_by_provider:
            try:
                unexpected.add(provider_to_internal_symbol(provider_symbol))
            except ProbeConfigurationError:
                unexpected.add(provider_symbol or "<missing-code>")
            continue
        grouped.setdefault(provider_symbol, []).append(row)

    duplicate_symbols = sorted(
        security_by_provider[symbol].symbol
        for symbol, values in grouped.items()
        if len(values) > 1
    )
    records: list[dict[str, object]] = []
    missing_required_fields: dict[str, list[str]] = {}
    for provider_symbol in sorted(grouped):
        row = grouped[provider_symbol][0]
        security = security_by_provider[provider_symbol]
        missing_fields = [
            field
            for field in SNAPSHOT_REQUIRED_FIELDS
            if field not in row or _is_missing(row[field])
        ]
        if missing_fields:
            missing_required_fields[security.symbol] = missing_fields
        state = market_states.get(provider_symbol, "UNKNOWN")
        provider_update_time = _parse_provider_update_time(row.get("update_time"))
        delay_ms = (
            _delay_ms(provider_update_time, received_at)
            if provider_update_time is not None
            else None
        )
        records.append(
            {
                "symbol": security.symbol,
                "provider_symbol": provider_symbol,
                "security_type": security.security_type.value,
                "name": _optional_text(row.get("name")),
                "provider_update_time": (
                    provider_update_time.isoformat()
                    if provider_update_time is not None
                    else None
                ),
                "received_at": _isoformat_utc(received_at),
                "delay_ms": delay_ms,
                "market_state": state,
                "freshness_assessment": _record_freshness_assessment(
                    market_state=state,
                    expect_live=expect_live,
                    provider_update_time=provider_update_time,
                    delay_ms=delay_ms,
                ).value,
                "last": _decimal_text(row.get("last_price")),
                "previous_close": _decimal_text(row.get("prev_close_price")),
                "open": _decimal_text(row.get("open_price")),
                "high": _decimal_text(row.get("high_price")),
                "low": _decimal_text(row.get("low_price")),
                "volume": _decimal_text(row.get("volume")),
                "turnover": _decimal_text(row.get("turnover")),
                "volume_unit": "unknown_requires_verification",
                "turnover_unit": "unknown_requires_verification",
                "suspended": _optional_bool(row.get("suspension")),
                "security_status": _optional_text(row.get("sec_status")),
            }
        )

    requested_provider_symbols = set(security_by_provider)
    missing_symbols = sorted(
        security_by_provider[symbol].symbol
        for symbol in requested_provider_symbols - set(grouped)
    )
    return {
        "records": sorted(records, key=lambda record: str(record["symbol"])),
        "missing_symbols": missing_symbols,
        "duplicate_symbols": duplicate_symbols,
        "unexpected_symbols": sorted(unexpected),
        "missing_required_fields": dict(sorted(missing_required_fields.items())),
        "returned_fields": sorted(returned_fields),
    }


def _assess_live_freshness(
    *,
    expect_live: bool,
    records: Sequence[Mapping[str, object]],
    requested_symbols: Sequence[str],
) -> dict[str, object]:
    records_by_symbol = {
        str(record["symbol"]): record for record in records
    }
    sample_coverage = {
        symbol: symbol in records_by_symbol for symbol in REQUIRED_SAMPLE_SYMBOLS
    }
    holding_coverage = {
        symbol: symbol in records_by_symbol for symbol in CRITICAL_HOLDING_SYMBOLS
    }
    states = {
        str(record.get("market_state", "UNKNOWN")) for record in records
    }
    outside_continuous_trading = (
        not records
        or bool(states - CONTINUOUS_TRADING_STATES)
    )
    if not expect_live:
        assessment = (
            FreshnessAssessment.NOT_VERIFIED_OUTSIDE_CONTINUOUS_TRADING
            if outside_continuous_trading
            else FreshnessAssessment.NOT_VERIFIED_EXPECT_LIVE_NOT_REQUESTED
        )
        verified = False
    else:
        time_checks_pass = all(
            record.get("provider_update_time") is not None
            and isinstance(record.get("delay_ms"), int | float)
            and cast(float, record["delay_ms"]) >= 0
            for record in records
        )
        verified = bool(
            records
            and not outside_continuous_trading
            and time_checks_pass
            and all(holding_coverage.values())
            and set(records_by_symbol) <= set(requested_symbols)
        )
        assessment = (
            FreshnessAssessment.LIVE_CHECKS_PASSED_SINGLE_SNAPSHOT_ONLY
            if verified
            else FreshnessAssessment.LIVE_CHECKS_FAILED
        )
    return {
        "freshness_assessment": assessment.value,
        "live_freshness_verified": verified,
        "required_sample_coverage": dict(sorted(sample_coverage.items())),
        "critical_holding_coverage": dict(sorted(holding_coverage.items())),
    }


def _record_freshness_assessment(
    *,
    market_state: str,
    expect_live: bool,
    provider_update_time: datetime | None,
    delay_ms: int | None,
) -> FreshnessAssessment:
    if market_state not in CONTINUOUS_TRADING_STATES:
        return FreshnessAssessment.NOT_VERIFIED_OUTSIDE_CONTINUOUS_TRADING
    if not expect_live:
        return FreshnessAssessment.NOT_VERIFIED_EXPECT_LIVE_NOT_REQUESTED
    if provider_update_time is None or delay_ms is None or delay_ms < 0:
        return FreshnessAssessment.LIVE_CHECKS_FAILED
    return FreshnessAssessment.LIVE_CHECKS_PASSED_SINGLE_SNAPSHOT_ONLY


def _probe_status(
    *,
    missing_symbols: Sequence[str],
    duplicate_symbols: Sequence[str],
    unexpected_symbols: Sequence[str],
    missing_required_fields: Mapping[str, Sequence[str]],
    errors: Sequence[Mapping[str, str]],
    expect_live: bool,
    live_freshness_verified: bool,
) -> ProbeStatus:
    if expect_live and not live_freshness_verified:
        return ProbeStatus.FAILED
    if (
        missing_symbols
        or duplicate_symbols
        or unexpected_symbols
        or missing_required_fields
        or errors
    ):
        return ProbeStatus.PARTIAL
    return ProbeStatus.SUCCESS


def _extract_market_states(
    rows: Sequence[Mapping[str, object]],
    *,
    requested_provider_symbols: set[str],
) -> dict[str, str]:
    states: dict[str, str] = {}
    for row in rows:
        provider_symbol = str(row.get("code", ""))
        if provider_symbol not in requested_provider_symbols:
            continue
        state = _normalize_market_state(row.get("market_state"))
        if provider_symbol not in states:
            states[provider_symbol] = state
    return dict(sorted(states.items()))


def _internal_market_states(
    market_states: Mapping[str, str],
) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for provider_symbol, state in market_states.items():
        try:
            internal_symbol = provider_to_internal_symbol(provider_symbol)
        except ProbeConfigurationError:
            continue
        normalized[internal_symbol] = state
    return dict(sorted(normalized.items()))


def _normalize_market_state(value: object) -> str:
    text = str(value or "UNKNOWN").strip()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text.upper() or "UNKNOWN"


def _parse_provider_update_time(value: object) -> datetime | None:
    if value is None or _is_missing(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TIMEZONE)
    return parsed


def _delay_ms(provider_time: datetime, received_at: datetime) -> int:
    return round(
        (
            received_at.astimezone(UTC)
            - provider_time.astimezone(UTC)
        ).total_seconds()
        * 1000
    )


def _decimal_text(value: object) -> str | None:
    if value is None or _is_missing(value):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return format(parsed, "f")


def _optional_text(value: object) -> str | None:
    if value is None or _is_missing(value):
        return None
    text = str(value).strip()
    return text or None


def _optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
    return None


def _is_missing(value: object) -> bool:
    if value is None:
        return True
    return isinstance(value, float) and math.isnan(value)


def _failed_report(
    *,
    client: QuoteClient,
    securities: Sequence[ProbeSecurity],
    host: str,
    port: int,
    expect_live: bool,
    started_at: datetime,
    completed_at: datetime,
    elapsed_ms: float,
    market_state_calls: int,
    snapshot_calls: int,
    market_states: Mapping[str, str],
    errors: Sequence[Mapping[str, str]],
) -> dict[str, object]:
    counts = Counter(security.security_type.value for security in securities)
    return {
        "schema_version": 1,
        "probe_name": "opend_a_share_realtime_capabilities",
        "provider": "futu_opend",
        "sdk_version": client.sdk_version,
        "python_version": sys.version.split()[0],
        "host": host,
        "port": port,
        "started_at": _isoformat_utc(started_at),
        "completed_at": _isoformat_utc(completed_at),
        "received_at": None,
        "elapsed_ms": elapsed_ms,
        "status": ProbeStatus.FAILED.value,
        "requested_count": len(securities),
        "stock_count": counts[SecurityType.STOCK.value],
        "etf_count": counts[SecurityType.ETF.value],
        "returned_count": 0,
        "requested_symbols": sorted(
            security.symbol for security in securities
        ),
        "records": [],
        "missing_symbols": [],
        "duplicate_symbols": [],
        "unexpected_symbols": [],
        "missing_required_fields": {},
        "returned_fields": [],
        "market_state": "UNKNOWN",
        "market_states": _internal_market_states(market_states),
        "freshness_assessment": (
            FreshnessAssessment.LIVE_CHECKS_FAILED.value
            if expect_live
            else FreshnessAssessment.NOT_VERIFIED_OUTSIDE_CONTINUOUS_TRADING.value
        ),
        "live_freshness_verified": False,
        "continuous_updates_verified": False,
        "required_sample_coverage": {
            symbol: False for symbol in REQUIRED_SAMPLE_SYMBOLS
        },
        "critical_holding_coverage": {
            symbol: False for symbol in CRITICAL_HOLDING_SYMBOLS
        },
        "expect_live": expect_live,
        "market_state_calls": market_state_calls,
        "snapshot_calls": snapshot_calls,
        "network_calls": market_state_calls + snapshot_calls,
        "errors": [dict(error) for error in errors],
        "notes": ["snapshot request failed; no quote coverage conclusion was generated"],
    }


def classify_error(
    error: Exception | str,
    *,
    operation: str,
) -> OpenDProbeError:
    if isinstance(error, OpenDProbeError):
        return error
    message = sanitize_error_message(str(error))
    lowered = message.lower()
    if isinstance(error, (TimeoutError,)) or any(
        phrase in lowered
        for phrase in ("timeout", "timed out", "超时")
    ):
        category = ErrorCategory.TIMEOUT
    elif isinstance(error, (ConnectionError, OSError)) or any(
        phrase in lowered
        for phrase in (
            "connection refused",
            "connect failed",
            "cannot connect",
            "opend is not started",
            "opend未启动",
            "无法连接",
        )
    ):
        category = (
            ErrorCategory.CONNECTION_REFUSED
            if isinstance(error, ConnectionRefusedError)
            or "connection refused" in lowered
            or "econnrefused" in lowered
            else ErrorCategory.OPEND_UNAVAILABLE
        )
    elif any(
        phrase in lowered
        for phrase in (
            "qot login",
            "quote login",
            "login failed",
            "not logged in",
            "行情登录失败",
            "未登录行情",
        )
    ):
        category = ErrorCategory.AUTHENTICATION_FAILED
    elif any(
        phrase in lowered
        for phrase in (
            "quote right",
            "permission denied",
            "no permission",
            "权限不足",
            "没有行情权限",
            "无行情权限",
        )
    ):
        category = ErrorCategory.PERMISSION_DENIED
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
        category = ErrorCategory.RATE_LIMITED
    elif isinstance(error, (TypeError, ValueError)):
        category = ErrorCategory.INVALID_RESPONSE
    elif isinstance(error, str):
        category = ErrorCategory.PROTOCOL_ERROR
    else:
        category = ErrorCategory.UNEXPECTED_ERROR
    return OpenDProbeError(category, operation, message or category.value)


def sanitize_error_message(message: str) -> str:
    without_controls = "".join(
        character
        for character in message
        if not unicodedata.category(character).startswith("C")
    )
    sanitized = PHONE_PATTERN.sub("[redacted-phone]", without_controls)
    sanitized = EMAIL_PATTERN.sub("[redacted-email]", sanitized)
    sanitized = BEARER_PATTERN.sub("Bearer [redacted-secret]", sanitized)
    sanitized = LABELED_SECRET_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[redacted-secret]",
        sanitized,
    )
    sanitized = LABELED_ACCOUNT_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[redacted-account]",
        sanitized,
    )
    sanitized = LONG_CREDENTIAL_PATTERN.sub("[redacted-secret]", sanitized)
    return " ".join(sanitized.split())[:MAX_ERROR_MESSAGE_LENGTH]


def _error_payload(error: OpenDProbeError) -> dict[str, str]:
    return {
        "category": error.category.value,
        "code": error.category.value,
        "operation": error.operation,
        "message": error.safe_message,
    }


def _table_to_records(
    table: object,
    *,
    operation: str,
) -> tuple[dict[str, object], ...]:
    raw_records: object
    if isinstance(table, (list, tuple)):
        raw_records = table
    else:
        to_dict = getattr(table, "to_dict", None)
        if not callable(to_dict):
            raise OpenDProbeError(
                ErrorCategory.INVALID_RESPONSE,
                operation,
                "OpenD success response is not a table",
            )
        try:
            raw_records = to_dict(orient="records")
        except Exception as error:
            raise classify_error(error, operation=operation) from error
    if not isinstance(raw_records, (list, tuple)):
        raise OpenDProbeError(
            ErrorCategory.INVALID_RESPONSE,
            operation,
            "OpenD table conversion did not return records",
        )
    records: list[dict[str, object]] = []
    for record in raw_records:
        if not isinstance(record, Mapping):
            raise OpenDProbeError(
                ErrorCategory.INVALID_RESPONSE,
                operation,
                "OpenD table contains a non-object record",
            )
        records.append({str(key): value for key, value in record.items()})
    return tuple(records)


def write_report(
    report: Mapping[str, object],
    project_root: Path,
) -> Path:
    output_path = project_root / REPORT_RELATIVE_PATH
    output_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    json.loads(serialized)
    output_path.write_text(serialized + "\n", encoding="utf-8")
    return output_path


def print_summary(
    report: Mapping[str, object],
) -> None:
    errors = cast(Sequence[Mapping[str, str]], report.get("errors", ()))
    error_counts = Counter(error["category"] for error in errors)
    print(
        json.dumps(
            {
                "status": report["status"],
                "requested_count": report["requested_count"],
                "returned_count": report["returned_count"],
                "missing_count": len(
                    cast(Sequence[object], report["missing_symbols"])
                ),
                "duplicate_count": len(
                    cast(Sequence[object], report["duplicate_symbols"])
                ),
                "unexpected_count": len(
                    cast(Sequence[object], report["unexpected_symbols"])
                ),
                "stock_count": report["stock_count"],
                "etf_count": report["etf_count"],
                "market_state": report["market_state"],
                "freshness_assessment": report["freshness_assessment"],
                "live_freshness_verified": report["live_freshness_verified"],
                "continuous_updates_verified": False,
                "snapshot_calls": report["snapshot_calls"],
                "market_state_calls": report["market_state_calls"],
                "network_calls": report["network_calls"],
                "error_counts": dict(sorted(error_counts.items())),
                "host": report["host"],
                "port": report["port"],
                "endpoint_reachable": report.get("endpoint_reachable"),
                "endpoint_preflight_calls": report.get(
                    "endpoint_preflight_calls", 0
                ),
                "recommendation": report.get("recommendation"),
                "output_path": REPORT_RELATIVE_PATH.as_posix(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _load_futu_module() -> object:
    try:
        return importlib.import_module("futu")
    except ImportError as error:
        raise ProbeConfigurationError(
            "futu-api is not installed; install the isolated Spike requirements"
        ) from error


def _default_client_factory(host: str, port: int) -> QuoteClient:
    return OpenDQuoteClientAdapter(host, port)


@dataclass(frozen=True)
class ParsedArguments:
    execute: bool
    expect_live: bool
    host: str
    port: int
    config_path: Path


def _parse_arguments(argv: Sequence[str] | None) -> ParsedArguments:
    parser = argparse.ArgumentParser(
        description="Probe Futu OpenD A-share snapshot capabilities."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the call plan without importing futu-api or connecting to OpenD",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="Execute one market-state call and one snapshot call",
    )
    parser.add_argument(
        "--expect-live",
        action="store_true",
        help="Require MORNING/AFTERNOON and validate a live single snapshot",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--config", type=Path, default=DEFAULT_WATCHLIST_PATH)
    namespace = parser.parse_args(argv)
    if namespace.expect_live and not namespace.execute:
        raise ProbeConfigurationError("--expect-live requires --execute")
    if not 1 <= namespace.port <= 65535:
        raise ProbeConfigurationError("port must be between 1 and 65535")
    return ParsedArguments(
        execute=bool(namespace.execute),
        expect_live=bool(namespace.expect_live),
        host=str(namespace.host),
        port=int(namespace.port),
        config_path=cast(Path, namespace.config),
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    project_root: Path = PROJECT_ROOT,
    client_factory: ClientFactory = _default_client_factory,
    endpoint_checker: EndpointChecker = check_opend_endpoint,
    now: Clock = lambda: datetime.now(UTC),
    monotonic: MonotonicClock = time.perf_counter,
) -> int:
    try:
        args = _parse_arguments(argv)
        config_path = (
            args.config_path
            if args.config_path.is_absolute()
            else project_root / args.config_path
        )
        securities = load_probe_securities(config_path)
    except ProbeConfigurationError as error:
        print(
            json.dumps(
                {
                    "status": "configuration_error",
                    "error": sanitize_error_message(str(error)),
                    "network_calls": 0,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    if not args.execute:
        print(
            json.dumps(
                build_dry_run(
                    securities,
                    host=args.host,
                    port=args.port,
                ),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    try:
        endpoint_checker(
            args.host,
            args.port,
            DEFAULT_CONNECT_TIMEOUT_SECONDS,
        )
    except Exception as error:  # noqa: BLE001 - socket errors vary by platform.
        classified = classify_error(error, operation="endpoint_preflight")
        failure = _connection_failure_report(
            securities=securities,
            host=args.host,
            port=args.port,
            expect_live=args.expect_live,
            error=classified,
            now=now,
            endpoint_preflight_calls=1,
        )
        write_report(failure, project_root)
        print_summary(failure)
        return 2

    try:
        client = client_factory(args.host, args.port)
    except Exception as error:  # noqa: BLE001 - connection errors vary by SDK version.
        classified = classify_error(error, operation="connect")
        failure = _connection_failure_report(
            securities=securities,
            host=args.host,
            port=args.port,
            expect_live=args.expect_live,
            error=classified,
            now=now,
            endpoint_preflight_calls=1,
        )
        write_report(failure, project_root)
        print_summary(failure)
        return 2

    close_error: OpenDProbeError | None = None
    try:
        report = run_probe(
            client,
            securities,
            host=args.host,
            port=args.port,
            expect_live=args.expect_live,
            now=now,
            monotonic=monotonic,
        )
        report["endpoint_reachable"] = True
        report["endpoint_preflight_calls"] = 1
        report["recommendation"] = None
    finally:
        try:
            client.close()
        except Exception as error:  # noqa: BLE001 - SDK close errors vary.
            close_error = classify_error(error, operation="close")

    if close_error is not None:
        errors = cast(list[dict[str, str]], report["errors"])
        errors.append(_error_payload(close_error))
        if report["status"] == ProbeStatus.SUCCESS.value:
            report["status"] = ProbeStatus.PARTIAL.value

    write_report(report, project_root)
    print_summary(report)
    return 2 if report["status"] == ProbeStatus.FAILED.value else 0


def _connection_failure_report(
    *,
    securities: Sequence[ProbeSecurity],
    host: str,
    port: int,
    expect_live: bool,
    error: OpenDProbeError,
    now: Clock,
    endpoint_preflight_calls: int,
) -> dict[str, object]:
    timestamp = _require_aware(now())

    class _UnavailableClient:
        sdk_version = "unavailable"

    report = _failed_report(
        client=cast(QuoteClient, _UnavailableClient()),
        securities=securities,
        host=host,
        port=port,
        expect_live=expect_live,
        started_at=timestamp,
        completed_at=timestamp,
        elapsed_ms=0.0,
        market_state_calls=0,
        snapshot_calls=0,
        market_states={},
        errors=(_error_payload(error),),
    )
    report["endpoint_reachable"] = False
    report["endpoint_preflight_calls"] = endpoint_preflight_calls
    report["recommendation"] = OPEND_START_RECOMMENDATION
    report["notes"] = [
        "OpenD endpoint preflight failed; the Futu SDK context was not created"
    ]
    return report


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProbeConfigurationError("probe clocks must return timezone-aware datetimes")
    return value


def _isoformat_utc(value: datetime) -> str:
    return _require_aware(value).astimezone(UTC).isoformat()


def _elapsed_ms(started: float, completed: float) -> float:
    return round(max(0.0, completed - started) * 1000, 3)


if __name__ == "__main__":
    raise SystemExit(main())
