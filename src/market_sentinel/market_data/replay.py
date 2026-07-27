from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from market_sentinel.domain.models import MarketPhase
from market_sentinel.domain.quotes import (
    MarketQuote,
    QualityIssue,
    QualitySeverity,
    QuoteBatch,
    QuoteFreshness,
    QuoteMarketState,
)
from market_sentinel.domain.security_data import (
    DataCompleteness,
    MarketDataErrorCategory,
    ProviderError,
)
from market_sentinel.market_data.base import QuoteMarketDataProvider
from market_sentinel.market_data.errors import MarketDataQualityError
from market_sentinel.market_data.opend import (
    CRITICAL_HOLDING_SYMBOLS,
    sanitize_opend_error,
    sanitize_opend_market_state,
)
from market_sentinel.market_data.shadow import (
    ShadowOutputError,
    write_shadow_report_atomic,
)
from market_sentinel.watchlist import WatchlistConfigurationError, WatchlistLoader

SUPPORTED_SNAPSHOT_SCHEMA_VERSION = 1
REPLAY_REPORT_SCHEMA_VERSION = 1
REPLAY_OUTPUT_DIR = Path("data/market-data/replays")
MAX_SNAPSHOT_BYTES = 50 * 1024 * 1024

_CANONICAL_SYMBOL = re.compile(r"^\d{6}\.(SH|SZ)$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

Clock = Callable[[], datetime]


class SnapshotReadError(Exception):
    def __init__(self, code: str, path: Path) -> None:
        self.code = code
        self.path = path
        super().__init__(f"snapshot {code}: {path.as_posix()}")


@dataclass(frozen=True)
class LoadedMarketSnapshot:
    input_path: Path
    input_provider: str
    original_requested_at: datetime
    original_completed_at: datetime
    original_market_state: QuoteMarketState
    original_freshness: QuoteFreshness
    original_completeness: DataCompleteness
    batch: QuoteBatch


class MarketSnapshotReader:
    """Strict JSON-only reader for provider-independent shadow snapshots."""

    def read(self, path: Path) -> LoadedMarketSnapshot:
        payload = self._read_json_object(path)
        self._require_schema_version(payload, path)

        input_provider = _safe_identifier(
            payload.get("provider"),
            code="invalid_provider",
            path=path,
        )
        original_requested_at = _aware_datetime(
            payload.get("requested_at"),
            code="invalid_requested_at",
            path=path,
        )
        original_completed_at = _aware_datetime(
            payload.get("completed_at"),
            code="invalid_completed_at",
            path=path,
        )
        if original_completed_at < original_requested_at:
            raise SnapshotReadError("invalid_snapshot_time_order", path)

        original_market_state = _enum_value(
            QuoteMarketState,
            payload.get("market_state"),
            code="invalid_market_state",
            path=path,
        )
        original_freshness = _enum_value(
            QuoteFreshness,
            payload.get("freshness_status"),
            code="invalid_freshness_status",
            path=path,
        )
        original_completeness = _enum_value(
            DataCompleteness,
            payload.get("completeness"),
            code="invalid_completeness",
            path=path,
        )
        requested_symbols = _symbol_sequence(
            payload.get("requested_symbols"),
            field="requested_symbols",
            path=path,
            required=True,
        )
        quote_rows = payload.get("quotes")
        if not isinstance(quote_rows, list):
            raise SnapshotReadError("quotes_must_be_a_list", path)

        (
            quotes,
            invalid_from_quotes,
            duplicate_from_quotes,
            unexpected_from_quotes,
            validation_issues,
        ) = self._parse_quotes(
            quote_rows,
            requested_symbols=set(requested_symbols),
            path=path,
        )

        missing = set(
            _symbol_sequence(payload.get("missing_symbols", []), field="missing_symbols", path=path)
        )
        stale = set(
            _symbol_sequence(payload.get("stale_symbols", []), field="stale_symbols", path=path)
        )
        invalid = set(
            _symbol_sequence(payload.get("invalid_symbols", []), field="invalid_symbols", path=path)
        )
        duplicate = set(
            _symbol_sequence(
                payload.get("duplicate_symbols", []),
                field="duplicate_symbols",
                path=path,
            )
        )
        unexpected = set(
            _symbol_sequence(
                payload.get("unexpected_symbols", []),
                field="unexpected_symbols",
                path=path,
            )
        )
        invalid.update(invalid_from_quotes)
        duplicate.update(duplicate_from_quotes)
        invalid.update(duplicate_from_quotes)
        unexpected.update(unexpected_from_quotes)

        requested_set = set(requested_symbols)
        if not (missing | stale | invalid | duplicate) <= requested_set:
            raise SnapshotReadError("unavailable_symbol_was_not_requested", path)
        if unexpected & requested_set:
            raise SnapshotReadError("unexpected_symbol_was_requested", path)

        accepted = {
            quote.symbol: quote
            for quote in quotes
            if quote.symbol not in missing | invalid | stale | duplicate
        }
        missing.update(requested_set - set(accepted) - invalid - stale)
        unavailable = missing | stale | invalid
        critical_missing = requested_set & CRITICAL_HOLDING_SYMBOLS & unavailable

        provider_errors = self._parse_provider_errors(
            payload.get("provider_errors"),
            path,
        )
        quality_issues = self._parse_quality_issues(payload, path)
        quality_issues.extend(validation_issues)
        quality_issues.extend(
            QualityIssue(
                code="critical_symbol_unavailable",
                severity=QualitySeverity.CRITICAL,
                message="critical holding has no usable quote in the snapshot",
                symbol=symbol,
            )
            for symbol in sorted(critical_missing)
            if not any(
                issue.symbol == symbol and issue.severity is QualitySeverity.CRITICAL
                for issue in quality_issues
            )
        )

        market_phase = self._market_phase(payload, tuple(accepted.values()), path)
        quote_sources = {quote.source for quote in accepted.values()}
        if len(quote_sources) > 1:
            raise SnapshotReadError("mixed_quote_sources", path)
        source = next(iter(quote_sources), input_provider)
        raw_market_states = _raw_market_states(payload.get("raw_market_state"), path)
        has_issues = bool(
            missing
            or stale
            or invalid
            or duplicate
            or unexpected
            or provider_errors
        )
        completeness = (
            DataCompleteness.FAILED
            if not accepted
            else DataCompleteness.PARTIAL
            if has_issues
            else DataCompleteness.COMPLETE
        )
        try:
            batch = QuoteBatch(
                requested_symbols=requested_symbols,
                quotes=tuple(accepted.values()),
                missing_symbols=tuple(missing),
                stale_symbols=tuple(stale),
                invalid_symbols=tuple(invalid),
                duplicate_symbols=tuple(duplicate),
                unexpected_symbols=tuple(unexpected),
                critical_missing_symbols=tuple(critical_missing),
                provider_errors=tuple(provider_errors),
                quality_issues=tuple(quality_issues),
                returned_count=sum(
                    isinstance(row, Mapping)
                    and row.get("symbol") in requested_set
                    for row in quote_rows
                ),
                completeness=completeness,
                coverage_ratio=Decimal(len(accepted)) / Decimal(len(requested_symbols)),
                source=source,
                market_phase=market_phase,
                market_state=original_market_state,
                raw_market_states=raw_market_states,
                freshness=original_freshness,
                requested_at=original_requested_at,
                completed_at=original_completed_at,
            )
        except ValidationError as error:
            raise SnapshotReadError("invalid_quote_batch", path) from error
        return LoadedMarketSnapshot(
            input_path=path,
            input_provider=input_provider,
            original_requested_at=original_requested_at,
            original_completed_at=original_completed_at,
            original_market_state=original_market_state,
            original_freshness=original_freshness,
            original_completeness=original_completeness,
            batch=batch,
        )

    def _read_json_object(self, path: Path) -> dict[str, object]:
        if not path.exists():
            raise SnapshotReadError("file_not_found", path)
        if path.is_symlink() or not path.is_file():
            raise SnapshotReadError("not_a_regular_file", path)
        try:
            size = path.stat().st_size
        except OSError as error:
            raise SnapshotReadError("file_stat_failed", path) from error
        if size == 0:
            raise SnapshotReadError("empty_file", path)
        if size > MAX_SNAPSHOT_BYTES:
            raise SnapshotReadError("file_too_large", path)
        try:
            raw_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise SnapshotReadError("file_read_failed", path) from error
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as error:
            raise SnapshotReadError("invalid_json", path) from error
        if not isinstance(payload, dict):
            raise SnapshotReadError("root_must_be_an_object", path)
        return {str(key): value for key, value in payload.items()}

    def _require_schema_version(self, payload: Mapping[str, object], path: Path) -> None:
        version = payload.get("schema_version")
        if type(version) is not int:
            raise SnapshotReadError("invalid_schema_version", path)
        if version != SUPPORTED_SNAPSHOT_SCHEMA_VERSION:
            raise SnapshotReadError("unsupported_schema_version", path)

    def _parse_quotes(
        self,
        rows: Sequence[object],
        *,
        requested_symbols: set[str],
        path: Path,
    ) -> tuple[
        tuple[MarketQuote, ...],
        set[str],
        set[str],
        set[str],
        list[QualityIssue],
    ]:
        accepted: dict[str, MarketQuote] = {}
        seen: set[str] = set()
        invalid: set[str] = set()
        duplicate: set[str] = set()
        unexpected: set[str] = set()
        issues: list[QualityIssue] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise SnapshotReadError("quote_must_be_an_object", path)
            symbol = _canonical_symbol(row.get("symbol"), "invalid_quote_symbol", path)
            if symbol in seen:
                duplicate.add(symbol)
                invalid.add(symbol)
                accepted.pop(symbol, None)
                continue
            seen.add(symbol)
            try:
                quote = MarketQuote.model_validate(dict(row))
                _validate_quote_identifiers(quote, path)
            except (ValidationError, SnapshotReadError):
                invalid.add(symbol)
                issues.append(
                    QualityIssue(
                        code="invalid_snapshot_quote",
                        severity=QualitySeverity.HIGH,
                        message="snapshot quote failed deterministic domain validation",
                        symbol=symbol,
                    )
                )
                continue
            if symbol not in requested_symbols:
                unexpected.add(symbol)
                continue
            accepted[symbol] = quote
        return (
            tuple(accepted.values()),
            invalid,
            duplicate,
            unexpected,
            issues,
        )

    def _parse_provider_errors(
        self,
        value: object,
        path: Path,
    ) -> list[ProviderError]:
        if not isinstance(value, list):
            raise SnapshotReadError("provider_errors_must_be_a_list", path)
        errors: list[ProviderError] = []
        for item in value:
            if not isinstance(item, Mapping):
                raise SnapshotReadError("provider_error_must_be_an_object", path)
            try:
                error = ProviderError.model_validate(dict(item))
            except ValidationError as validation_error:
                raise SnapshotReadError("invalid_provider_error", path) from validation_error
            safe_code = (
                _safe_identifier(error.code, code="invalid_provider_error_code", path=path)
                if error.code is not None
                else None
            )
            errors.append(
                error.model_copy(
                    update={
                        "code": safe_code,
                        "message": sanitize_opend_error(error.message),
                    }
                )
            )
        return errors

    def _parse_quality_issues(
        self,
        payload: Mapping[str, object],
        path: Path,
    ) -> list[QualityIssue]:
        result = payload.get("quality_gate_result")
        if not isinstance(result, Mapping):
            raise SnapshotReadError("quality_gate_result_must_be_an_object", path)
        raw_issues = result.get("quality_issues")
        if not isinstance(raw_issues, list):
            raise SnapshotReadError("quality_issues_must_be_a_list", path)
        issues: list[QualityIssue] = []
        for item in raw_issues:
            if not isinstance(item, Mapping):
                raise SnapshotReadError("quality_issue_must_be_an_object", path)
            try:
                issue = QualityIssue.model_validate(dict(item))
            except ValidationError as validation_error:
                raise SnapshotReadError("invalid_quality_issue", path) from validation_error
            safe_code = _safe_identifier(
                issue.code,
                code="invalid_quality_issue_code",
                path=path,
            )
            issues.append(
                issue.model_copy(
                    update={
                        "code": safe_code,
                        "message": sanitize_opend_error(issue.message),
                    }
                )
            )
        return issues

    def _market_phase(
        self,
        payload: Mapping[str, object],
        quotes: tuple[MarketQuote, ...],
        path: Path,
    ) -> MarketPhase:
        raw_phase = payload.get("market_phase")
        if raw_phase is not None:
            phase = _enum_value(
                MarketPhase,
                raw_phase,
                code="invalid_market_phase",
                path=path,
            )
        else:
            phases = {quote.market_phase for quote in quotes}
            if len(phases) != 1:
                raise SnapshotReadError("market_phase_missing_or_inconsistent", path)
            phase = next(iter(phases))
        if any(quote.market_phase is not phase for quote in quotes):
            raise SnapshotReadError("quote_market_phase_mismatch", path)
        return phase


class SnapshotReplayMarketDataProvider(QuoteMarketDataProvider):
    """Offline quote provider backed by one validated snapshot document."""

    def __init__(
        self,
        snapshot: LoadedMarketSnapshot,
        *,
        critical_symbols: Sequence[str] = tuple(CRITICAL_HOLDING_SYMBOLS),
        now: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self.snapshot = snapshot
        self._critical_symbols = frozenset(critical_symbols)
        self._now = now

    async def get_quotes(
        self,
        symbols: Sequence[str],
        phase: MarketPhase,
    ) -> QuoteBatch:
        requested = _validate_requested_symbols(symbols)
        source_batch = self.snapshot.batch
        if phase is not source_batch.market_phase:
            raise MarketDataQualityError(
                "replay phase must match the original snapshot phase"
            )

        replay_requested_at = _utc_now(self._now)
        source_quotes = {quote.symbol: quote for quote in source_batch.quotes}
        requested_set = set(requested)
        quotes = tuple(
            source_quotes[symbol]
            for symbol in requested
            if symbol in source_quotes
        )
        invalid = requested_set & set(source_batch.invalid_symbols)
        stale = requested_set & set(source_batch.stale_symbols)
        duplicate = requested_set & set(source_batch.duplicate_symbols)
        invalid.update(duplicate)
        missing = requested_set - {quote.symbol for quote in quotes} - invalid - stale
        unexpected = (
            set(source_batch.requested_symbols) - requested_set
        ) | (set(source_batch.unexpected_symbols) - requested_set)
        unavailable = missing | stale | invalid
        critical_missing = requested_set & self._critical_symbols & unavailable
        provider_errors = source_batch.provider_errors
        quality_issues = tuple(
            issue
            for issue in source_batch.quality_issues
            if issue.symbol is None or issue.symbol in requested_set
        ) + tuple(
            QualityIssue(
                code="critical_symbol_unavailable",
                severity=QualitySeverity.CRITICAL,
                message="critical holding has no usable quote in replay",
                symbol=symbol,
            )
            for symbol in sorted(critical_missing)
            if not any(
                issue.symbol == symbol and issue.severity is QualitySeverity.CRITICAL
                for issue in source_batch.quality_issues
            )
        )
        has_issues = bool(
            missing
            or stale
            or invalid
            or duplicate
            or unexpected
            or provider_errors
        )
        completeness = (
            DataCompleteness.FAILED
            if not quotes
            else DataCompleteness.PARTIAL
            if has_issues
            else DataCompleteness.COMPLETE
        )
        replayed_at = _utc_now(self._now)
        return QuoteBatch(
            requested_symbols=requested,
            quotes=quotes,
            missing_symbols=tuple(missing),
            stale_symbols=tuple(stale),
            invalid_symbols=tuple(invalid),
            duplicate_symbols=tuple(duplicate),
            unexpected_symbols=tuple(unexpected),
            critical_missing_symbols=tuple(critical_missing),
            provider_errors=provider_errors,
            quality_issues=quality_issues,
            returned_count=len(quotes),
            snapshot_calls=0,
            market_state_calls=0,
            network_calls=0,
            completeness=completeness,
            coverage_ratio=Decimal(len(quotes)) / Decimal(len(requested)),
            source=source_batch.source,
            market_phase=source_batch.market_phase,
            market_state=source_batch.market_state,
            raw_market_states=source_batch.raw_market_states,
            freshness=QuoteFreshness.REPLAY,
            requested_at=replay_requested_at,
            completed_at=replayed_at,
        )


async def run_market_data_replay_command(
    args: argparse.Namespace,
    *,
    reader: MarketSnapshotReader | None = None,
    watchlist_loader: WatchlistLoader | None = None,
    provider_factory: Callable[
        [LoadedMarketSnapshot, Clock],
        SnapshotReplayMarketDataProvider,
    ] = lambda snapshot, clock: SnapshotReplayMarketDataProvider(
        snapshot,
        now=clock,
    ),
    output_dir: Path | None = None,
    now: Clock = lambda: datetime.now(UTC),
) -> int:
    input_path = Path(args.input)
    config_path = args.config or Path(
        os.environ.get("WATCHLIST_CONFIG_PATH", "config/watchlist.yaml")
    )
    try:
        watchlist = (watchlist_loader or WatchlistLoader()).load(config_path)
    except WatchlistConfigurationError as error:
        _print_json(
            _replay_failure_summary(
                input_path=input_path,
                category="configuration",
                error_count=len(error.issues),
            )
        )
        return 2

    requested_symbols = tuple(
        security.symbol
        for security in watchlist.securities
        if security.enabled
    )
    try:
        snapshot = (reader or MarketSnapshotReader()).read(input_path)
    except SnapshotReadError as error:
        _print_json(
            _replay_failure_summary(
                input_path=input_path,
                category=error.code,
            )
        )
        return 2

    provider = provider_factory(snapshot, now)
    try:
        batch = await provider.get_quotes(
            requested_symbols,
            snapshot.batch.market_phase,
        )
    except Exception as error:  # noqa: BLE001 - replay provider remains injectable.
        _print_json(
            _replay_failure_summary(
                input_path=input_path,
                input_provider=snapshot.input_provider,
                requested_count=len(requested_symbols),
                category=_safe_exception_category(error),
            )
        )
        return 2

    output_path: Path | None = None
    if bool(args.write_report):
        report = build_replay_report(snapshot, batch)
        try:
            output_path = write_shadow_report_atomic(
                output_dir or REPLAY_OUTPUT_DIR,
                "replay",
                batch.completed_at,
                report,
            )
        except (OSError, ShadowOutputError):
            summary = build_replay_summary(snapshot, batch, output_path=None)
            summary["status"] = "failed"
            counts = Counter(
                error.category.value for error in batch.provider_errors
            )
            counts["output_write_failed"] += 1
            summary["provider_error_counts"] = dict(sorted(counts.items()))
            _print_json(summary)
            return 2

    _print_json(build_replay_summary(snapshot, batch, output_path=output_path))
    return 2 if batch.completeness is DataCompleteness.FAILED else 0


def build_replay_summary(
    snapshot: LoadedMarketSnapshot,
    batch: QuoteBatch,
    *,
    output_path: Path | None,
) -> dict[str, object]:
    return {
        "status": batch.completeness.value,
        "data_mode": "replay",
        "input_path": snapshot.input_path.as_posix(),
        "input_provider": snapshot.input_provider,
        "requested_count": len(batch.requested_symbols),
        "returned_count": batch.returned_count,
        "valid_quote_count": len(batch.quotes),
        "invalid_quote_count": len(batch.invalid_symbols),
        "missing_count": len(batch.missing_symbols),
        "duplicate_count": len(batch.duplicate_symbols),
        "unexpected_count": len(batch.unexpected_symbols),
        "critical_missing_symbols": list(batch.critical_missing_symbols),
        "completeness": batch.completeness.value,
        "original_market_state": snapshot.original_market_state.value,
        "original_freshness_status": snapshot.original_freshness.value,
        "original_completeness": snapshot.original_completeness.value,
        "replayed_at": batch.completed_at.isoformat(),
        "provider_error_counts": dict(
            sorted(Counter(error.category.value for error in batch.provider_errors).items())
        ),
        "output_path": output_path.as_posix() if output_path else None,
        "network_calls": 0,
    }


def build_replay_report(
    snapshot: LoadedMarketSnapshot,
    batch: QuoteBatch,
) -> dict[str, object]:
    return {
        "schema_version": REPLAY_REPORT_SCHEMA_VERSION,
        "data_mode": "replay",
        "input_path": snapshot.input_path.as_posix(),
        "input_provider": snapshot.input_provider,
        "input_snapshot_time": snapshot.original_completed_at.isoformat(),
        "original_requested_at": snapshot.original_requested_at.isoformat(),
        "original_completed_at": snapshot.original_completed_at.isoformat(),
        "original_market_state": snapshot.original_market_state.value,
        "original_freshness_status": snapshot.original_freshness.value,
        "replayed_at": batch.completed_at.isoformat(),
        "requested_symbols": list(batch.requested_symbols),
        "returned_symbols": [quote.symbol for quote in batch.quotes],
        "quotes": [quote.model_dump(mode="json") for quote in batch.quotes],
        "missing_symbols": list(batch.missing_symbols),
        "invalid_symbols": list(batch.invalid_symbols),
        "duplicate_symbols": list(batch.duplicate_symbols),
        "unexpected_symbols": list(batch.unexpected_symbols),
        "critical_missing_symbols": list(batch.critical_missing_symbols),
        "provider_errors": [
            {
                "category": error.category.value,
                "code": error.code,
                "message": sanitize_opend_error(error.message),
                "symbol": error.symbol,
            }
            for error in batch.provider_errors
        ],
        "quality_gate_result": {
            "status": batch.completeness.value,
            "data_freshness": QuoteFreshness.REPLAY.value,
            "coverage_ratio": format(batch.coverage_ratio, "f"),
            "valid_quote_count": len(batch.quotes),
            "invalid_quote_count": len(batch.invalid_symbols),
            "quality_issues": [
                {
                    "code": issue.code,
                    "severity": issue.severity.value,
                    "message": sanitize_opend_error(issue.message),
                    "symbol": issue.symbol,
                }
                for issue in batch.quality_issues
            ],
        },
        "completeness": batch.completeness.value,
        "network_calls": 0,
    }


def _replay_failure_summary(
    *,
    input_path: Path,
    category: str,
    input_provider: str | None = None,
    requested_count: int = 0,
    error_count: int = 1,
) -> dict[str, object]:
    return {
        "status": "failed",
        "data_mode": "replay",
        "input_path": input_path.as_posix(),
        "input_provider": input_provider,
        "requested_count": requested_count,
        "returned_count": 0,
        "valid_quote_count": 0,
        "invalid_quote_count": 0,
        "missing_count": 0,
        "duplicate_count": 0,
        "unexpected_count": 0,
        "critical_missing_symbols": [],
        "completeness": "failed",
        "original_market_state": None,
        "original_freshness_status": None,
        "replayed_at": None,
        "provider_error_counts": {category: error_count},
        "output_path": None,
        "network_calls": 0,
    }


def _symbol_sequence(
    value: object,
    *,
    field: str,
    path: Path,
    required: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise SnapshotReadError(f"{field}_must_be_a_list", path)
    symbols = tuple(
        _canonical_symbol(item, f"invalid_{field}_symbol", path)
        for item in value
    )
    if required and not symbols:
        raise SnapshotReadError(f"{field}_must_not_be_empty", path)
    if len(symbols) != len(set(symbols)):
        raise SnapshotReadError(f"{field}_contains_duplicates", path)
    return tuple(sorted(symbols))


def _canonical_symbol(value: object, code: str, path: Path) -> str:
    if not isinstance(value, str) or _CANONICAL_SYMBOL.fullmatch(value) is None:
        raise SnapshotReadError(code, path)
    return value


def _safe_identifier(value: object, *, code: str, path: Path) -> str:
    if not isinstance(value, str) or _SAFE_IDENTIFIER.fullmatch(value) is None:
        raise SnapshotReadError(code, path)
    if sanitize_opend_error(value) != value:
        raise SnapshotReadError(code, path)
    return value


def _aware_datetime(value: object, *, code: str, path: Path) -> datetime:
    if not isinstance(value, str):
        raise SnapshotReadError(code, path)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise SnapshotReadError(code, path) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SnapshotReadError(code, path)
    return parsed.astimezone(UTC)


def _enum_value[
    EnumValue: (
        MarketPhase,
        QuoteMarketState,
        QuoteFreshness,
        DataCompleteness,
    )
](
    enum_type: type[EnumValue],
    value: object,
    *,
    code: str,
    path: Path,
) -> EnumValue:
    if not isinstance(value, str):
        raise SnapshotReadError(code, path)
    try:
        return enum_type(value)
    except (TypeError, ValueError) as error:
        raise SnapshotReadError(code, path) from error


def _raw_market_states(value: object, path: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise SnapshotReadError("raw_market_state_must_be_an_object", path)
    states: list[str] = []
    for raw_state, count in value.items():
        if type(count) is not int or count < 0 or count > 10000:
            raise SnapshotReadError("invalid_raw_market_state_count", path)
        cleaned = sanitize_opend_market_state(raw_state)
        if not cleaned:
            raise SnapshotReadError("invalid_raw_market_state", path)
        states.extend([cleaned] * count)
    return tuple(sorted(states))


def _validate_quote_identifiers(quote: MarketQuote, path: Path) -> None:
    _safe_identifier(quote.source, code="invalid_quote_source", path=path)
    if quote.provider_symbol is not None:
        _safe_identifier(
            quote.provider_symbol,
            code="invalid_provider_symbol",
            path=path,
        )


def _validate_requested_symbols(symbols: Sequence[str]) -> tuple[str, ...]:
    requested = tuple(symbols)
    if not requested:
        raise MarketDataQualityError("at least one replay symbol is required")
    if len(requested) != len(set(requested)):
        raise MarketDataQualityError("replay symbols must not contain duplicates")
    if any(_CANONICAL_SYMBOL.fullmatch(symbol) is None for symbol in requested):
        raise MarketDataQualityError("replay symbols must use canonical A-share codes")
    return tuple(sorted(requested))


def _utc_now(clock: Clock) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise MarketDataQualityError("replay clock must be timezone-aware")
    return value.astimezone(UTC)


def _safe_exception_category(error: BaseException) -> str:
    if isinstance(error, MarketDataQualityError):
        return "quality"
    return MarketDataErrorCategory.PROVIDER.value


def _print_json(payload: Mapping[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
