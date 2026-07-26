from __future__ import annotations

import argparse
import importlib
import json
import math
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Any, cast

from pydantic import SecretStr

PROJECT_ROOT = Path(__file__).resolve().parents[3]
REPORT_RELATIVE_PATH = Path("data/spikes/tushare/free-tier-capabilities.json")
MAX_INSPECTION_ROWS = 20
MAX_ERROR_MESSAGE_LENGTH = 500
REQUEST_PAUSE_SECONDS = 0.25

STOCK_CODE_PATTERN = re.compile(r"^\d{6}\.(?:SH|SZ)$")
PHONE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
LABELED_SECRET_PATTERN = re.compile(
    r"(?i)\b(token|api[_ -]?key|authorization|cookie|password)\b"
    r"(\s*[:=]\s*|\s+)([^\s,;]+)"
)
LABELED_ACCOUNT_PATTERN = re.compile(
    r"(?i)(?<!\w)(account(?:[_ -]?(?:id|name))?|"
    r"user(?:[_ -]?(?:id|name))?|账号|账户|用户(?:id)?)"
    r"(\s*[:=：]\s*|\s+)([^\s,;]+)"
)
BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
LONG_CREDENTIAL_PATTERN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_-]{20,}(?![A-Za-z0-9])")

TIME_FIELD_NAMES = (
    "trade_time",
    "time",
    "trade_date",
    "cal_date",
    "list_date",
    "found_date",
)
SOURCE_TIME_FIELD_NAMES = ("trade_time", "time")


class ProbeStatus(StrEnum):
    SUCCESS = "success"
    PERMISSION_DENIED = "permission_denied"
    AUTHENTICATION_FAILED = "authentication_failed"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    INVALID_REQUEST = "invalid_request"
    INVALID_RESPONSE = "invalid_response"
    UNAVAILABLE = "unavailable"
    UNEXPECTED_ERROR = "unexpected_error"


class ProbeConfigurationError(Exception):
    """A configuration error whose message is safe to show to the user."""


class InvalidProbeResponseError(Exception):
    """The SDK returned a value that is not a DataFrame-like table."""


@dataclass(frozen=True)
class ProbeParameters:
    sh_stock: str
    sz_stock: str
    sh_etf: str
    index: str
    trade_date: str

    @property
    def stock_pair(self) -> str:
        return f"{self.sh_stock},{self.sz_stock}"


@dataclass(frozen=True)
class CapabilitySpec:
    capability_name: str
    api_name: str
    request_kwargs: Mapping[str, str]
    sample_symbol: str | None
    document_url: str
    price_fields: tuple[str, ...] = ()
    volume_fields: tuple[str, ...] = ()
    turnover_fields: tuple[str, ...] = ()
    official_units: Mapping[str, str] | None = None
    notes: str = ""
    suspension_or_no_trade_behavior: str = "unknown_requires_verification"


def build_capability_specs(parameters: ProbeParameters) -> tuple[CapabilitySpec, ...]:
    stock_daily_fields = "ts_code,trade_date,open,high,low,close,pre_close,vol,amount"
    realtime_fields = "ts_code,trade_time,pre_close,open,high,low,close,vol,amount"
    return (
        CapabilitySpec(
            capability_name="trading_calendar",
            api_name="trade_cal",
            request_kwargs={
                "exchange": "SSE",
                "start_date": parameters.trade_date,
                "end_date": parameters.trade_date,
                "fields": "exchange,cal_date,is_open,pretrade_date",
            },
            sample_symbol=None,
            document_url="https://tushare.pro/document/2?doc_id=26",
            notes="Calendar date is not an exchange quote source timestamp.",
        ),
        CapabilitySpec(
            capability_name="a_share_stock_basic",
            api_name="stock_basic",
            request_kwargs={
                "ts_code": parameters.sh_stock,
                "fields": (
                    "ts_code,symbol,name,market,exchange,curr_type,list_status,list_date"
                ),
            },
            sample_symbol=parameters.sh_stock,
            document_url="https://tushare.pro/document/1?doc_id=25",
            notes=(
                "The Shanghai sample covers A-share security basics; the Shenzhen sample "
                "is covered by the combined daily and real-time requests."
            ),
        ),
        CapabilitySpec(
            capability_name="a_share_stock_daily",
            api_name="daily",
            request_kwargs={
                "ts_code": parameters.stock_pair,
                "start_date": parameters.trade_date,
                "end_date": parameters.trade_date,
                "fields": stock_daily_fields,
            },
            sample_symbol=parameters.stock_pair,
            document_url="https://tushare.pro/document/1?doc_id=27",
            price_fields=("open", "high", "low", "close", "pre_close"),
            volume_fields=("vol",),
            turnover_fields=("amount",),
            official_units={"vol": "lot", "amount": "CNY_thousand"},
            notes="Official daily documentation describes post-close unadjusted data.",
            suspension_or_no_trade_behavior=(
                "official_documentation_says_suspended_dates_return_no_daily_row"
            ),
        ),
        CapabilitySpec(
            capability_name="etf_basic",
            api_name="fund_basic",
            request_kwargs={
                "ts_code": parameters.sh_etf,
                "market": "E",
                "status": "L",
                "fields": "ts_code,name,fund_type,type,status,market,list_date",
            },
            sample_symbol=parameters.sh_etf,
            document_url="https://tushare.pro/document/1?doc_id=19",
        ),
        CapabilitySpec(
            capability_name="etf_daily",
            api_name="fund_daily",
            request_kwargs={
                "ts_code": parameters.sh_etf,
                "start_date": parameters.trade_date,
                "end_date": parameters.trade_date,
                "fields": stock_daily_fields,
            },
            sample_symbol=parameters.sh_etf,
            document_url="https://tushare.pro/document/2?doc_id=127",
            price_fields=("open", "high", "low", "close", "pre_close"),
            volume_fields=("vol",),
            turnover_fields=("amount",),
            official_units={"vol": "lot", "amount": "CNY_thousand"},
            notes="Official ETF daily documentation describes post-close data.",
        ),
        CapabilitySpec(
            capability_name="etf_realtime_snapshot",
            api_name="rt_etf_k",
            request_kwargs={
                "ts_code": parameters.sh_etf,
                "topic": "HQ_FND_TICK",
                "fields": realtime_fields,
            },
            sample_symbol=parameters.sh_etf,
            document_url="https://tushare.pro/document/2?doc_id=400",
            price_fields=("open", "high", "low", "close", "pre_close"),
            volume_fields=("vol",),
            turnover_fields=("amount",),
            official_units={"vol": "share", "amount": "CNY"},
            notes="Shanghai ETF requests require topic=HQ_FND_TICK.",
        ),
        CapabilitySpec(
            capability_name="index_basic",
            api_name="index_basic",
            request_kwargs={
                "ts_code": parameters.index,
                "fields": (
                    "ts_code,name,fullname,market,publisher,index_type,category,list_date"
                ),
            },
            sample_symbol=parameters.index,
            document_url="https://tushare.pro/document/2?doc_id=94",
        ),
        CapabilitySpec(
            capability_name="index_daily",
            api_name="index_daily",
            request_kwargs={
                "ts_code": parameters.index,
                "start_date": parameters.trade_date,
                "end_date": parameters.trade_date,
                "fields": stock_daily_fields,
            },
            sample_symbol=parameters.index,
            document_url="https://tushare.pro/document/1?doc_id=95",
            price_fields=("open", "high", "low", "close", "pre_close"),
            volume_fields=("vol",),
            turnover_fields=("amount",),
            official_units={"vol": "lot", "amount": "CNY_thousand"},
            notes="Index prices are index points rather than CNY security prices.",
        ),
        CapabilitySpec(
            capability_name="index_realtime_snapshot",
            api_name="rt_idx_k",
            request_kwargs={"ts_code": parameters.index, "fields": realtime_fields},
            sample_symbol=parameters.index,
            document_url="https://tushare.pro/document/2?doc_id=403",
            price_fields=("open", "high", "low", "close", "pre_close"),
            volume_fields=("vol",),
            turnover_fields=("amount",),
            official_units={
                "vol": "unknown_requires_verification",
                "amount": "CNY",
            },
            notes="Official real-time index documentation does not define the volume unit.",
        ),
        CapabilitySpec(
            capability_name="stock_realtime_snapshot",
            api_name="rt_k",
            request_kwargs={"ts_code": parameters.stock_pair, "fields": realtime_fields},
            sample_symbol=parameters.stock_pair,
            document_url="https://tushare.pro/document/2?doc_id=372",
            price_fields=("open", "high", "low", "close", "pre_close"),
            volume_fields=("vol",),
            turnover_fields=("amount",),
            official_units={"vol": "share", "amount": "CNY"},
        ),
        CapabilitySpec(
            capability_name="stock_realtime_minute",
            api_name="rt_min",
            request_kwargs={"ts_code": parameters.stock_pair, "freq": "1MIN"},
            sample_symbol=parameters.stock_pair,
            document_url="https://tushare.pro/document/2?doc_id=374",
            price_fields=("open", "high", "low", "close"),
            volume_fields=("vol",),
            turnover_fields=("amount",),
            official_units={"vol": "share", "amount": "CNY"},
            notes="The official API supports comma-separated stock codes and uppercase 1MIN.",
        ),
        CapabilitySpec(
            capability_name="opening_auction",
            api_name="stk_auction",
            request_kwargs={
                "ts_code": parameters.sh_stock,
                "trade_date": parameters.trade_date,
                "fields": "ts_code,trade_date,price,vol,amount,pre_close",
            },
            sample_symbol=parameters.sh_stock,
            document_url="https://tushare.pro/document/2?doc_id=369",
            price_fields=("price", "pre_close"),
            volume_fields=("vol",),
            turnover_fields=("amount",),
            official_units={"vol": "share", "amount": "CNY"},
            notes=(
                "Official documentation says same-day results are available around "
                "09:26-09:29; this is not the full 09:15-09:25 auction path."
            ),
        ),
    )


def load_token_from_project_env(project_root: Path) -> SecretStr:
    env_path = project_root / ".env"
    if not env_path.is_file():
        raise ProbeConfigurationError(
            "TUSHARE_TOKEN is missing: project-root .env does not exist"
        )

    found_value: str | None = None
    with env_path.open(encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line.removeprefix("export ").lstrip()
            key, separator, value = line.partition("=")
            if separator and key.strip() == "TUSHARE_TOKEN":
                if found_value is not None:
                    raise ProbeConfigurationError(
                        "TUSHARE_TOKEN is defined more than once in project-root .env"
                    )
                found_value = _unquote_dotenv_value(value.strip())

    if not found_value:
        raise ProbeConfigurationError(
            "TUSHARE_TOKEN is missing or empty in project-root .env"
        )
    return SecretStr(found_value)


def _unquote_dotenv_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def sanitize_error_message(error: BaseException, secret_values: Sequence[str] = ()) -> str:
    message = str(error)
    for secret in secret_values:
        if secret:
            message = message.replace(secret, "[redacted-secret]")
    message = LABELED_SECRET_PATTERN.sub(r"\1=[redacted-secret]", message)
    message = LABELED_ACCOUNT_PATTERN.sub(r"\1=[redacted-account]", message)
    message = BEARER_PATTERN.sub("Bearer [redacted-secret]", message)
    message = PHONE_PATTERN.sub("[redacted-phone]", message)
    message = EMAIL_PATTERN.sub("[redacted-email]", message)
    message = LONG_CREDENTIAL_PATTERN.sub("[redacted-secret]", message)
    message = " ".join(message.split())
    if not message:
        message = error.__class__.__name__
    return message[:MAX_ERROR_MESSAGE_LENGTH]


def classify_error(error: BaseException) -> ProbeStatus:
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
        "token expired",
        "authentication failed",
        "unauthorized",
        "认证失败",
        "鉴权失败",
        "未提供token",
        "token为空",
    )
    permission_markers = (
        "没有访问该接口的权限",
        "无权限",
        "权限不足",
        "积分不足",
        "permission denied",
        "forbidden",
        "code 2002",
        "code=2002",
    )
    rate_limit_markers = (
        "每分钟最多",
        "访问频次",
        "频率限制",
        "rate limit",
        "too many requests",
        "429",
    )
    invalid_request_markers = (
        "参数错误",
        "参数不能为空",
        "必须输入",
        "接口不存在",
        "invalid request",
        "invalid parameter",
        "bad request",
    )
    unavailable_markers = (
        "service unavailable",
        "服务不可用",
        "系统维护",
        "connection refused",
        "connection reset",
        "connectionerror",
        "network is unreachable",
    )

    if isinstance(error, TimeoutError) or "timeout" in text or "timed out" in text:
        return ProbeStatus.TIMEOUT
    if any(marker in text for marker in authentication_markers):
        return ProbeStatus.AUTHENTICATION_FAILED
    dynamic_interface_permission_error = (
        "没有接口" in text and "访问权限" in text
    )
    if dynamic_interface_permission_error or any(
        marker in text for marker in permission_markers
    ):
        return ProbeStatus.PERMISSION_DENIED
    if any(marker in text for marker in rate_limit_markers):
        return ProbeStatus.RATE_LIMITED
    if isinstance(error, InvalidProbeResponseError):
        return ProbeStatus.INVALID_RESPONSE
    if any(marker in text for marker in invalid_request_markers):
        return ProbeStatus.INVALID_REQUEST
    if isinstance(error, ConnectionError) or any(
        marker in text for marker in unavailable_markers
    ):
        return ProbeStatus.UNAVAILABLE
    return ProbeStatus.UNEXPECTED_ERROR


def probe_capability(
    client: object,
    spec: CapabilitySpec,
    token: SecretStr,
    *,
    monotonic: Callable[[], float] = time.perf_counter,
) -> dict[str, object]:
    started = monotonic()
    try:
        api = cast(Callable[..., object], getattr(client, spec.api_name))
        table = api(**dict(spec.request_kwargs))
        elapsed_ms = round((monotonic() - started) * 1000, 3)
        return _success_metadata(spec, table, elapsed_ms)
    except Exception as error:  # noqa: BLE001 - every provider error must become safe metadata
        elapsed_ms = round((monotonic() - started) * 1000, 3)
        status = classify_error(error)
        return _base_result(
            spec,
            status=status,
            elapsed_ms=elapsed_ms,
            error_category=status.value,
            error_message=sanitize_error_message(
                error, (token.get_secret_value(),)
            ),
        )


def _success_metadata(
    spec: CapabilitySpec, table: object, elapsed_ms: float
) -> dict[str, object]:
    columns = _extract_columns(table)
    try:
        row_count = len(cast(Any, table))
    except Exception as error:
        raise InvalidProbeResponseError("response does not expose a valid row count") from error
    if not isinstance(row_count, int) or row_count < 0:
        raise InvalidProbeResponseError("response row count is invalid")

    records = _extract_inspection_records(table)
    returned_fields = sorted(columns)
    detected_time_fields = [
        field for field in TIME_FIELD_NAMES if field in returned_fields
    ]
    source_time_fields = [
        field for field in SOURCE_TIME_FIELD_NAMES if field in returned_fields
    ]
    price_fields = [field for field in spec.price_fields if field in returned_fields]
    volume_fields = [field for field in spec.volume_fields if field in returned_fields]
    turnover_fields = [
        field for field in spec.turnover_fields if field in returned_fields
    ]

    result = _base_result(
        spec,
        status=ProbeStatus.SUCCESS,
        elapsed_ms=elapsed_ms,
        error_category=None,
        error_message=None,
    )
    result.update(
        {
            "row_count": row_count,
            "returned_fields": returned_fields,
            "has_source_time": bool(source_time_fields),
            "detected_time_fields": detected_time_fields,
            "price_fields": price_fields,
            "volume_fields": volume_fields,
            "turnover_fields": turnover_fields,
            "field_units": {
                field: _unit_for_field(spec, field)
                for field in (*price_fields, *volume_fields, *turnover_fields)
            },
            "symbol_formats": _detect_symbol_formats(records),
            "time_field_formats": _detect_time_formats(
                records, detected_time_fields
            ),
            "returned_sorting": _detect_sorting(records, detected_time_fields),
            "missing_value_representation": _detect_missing_values(records),
            "suspension_or_no_trade_behavior": (
                spec.suspension_or_no_trade_behavior
            ),
            "inspection_scope": f"first_{MAX_INSPECTION_ROWS}_rows_metadata_only",
        }
    )
    if row_count == 0:
        result["notes"] = _join_notes(spec.notes, "empty_response")
    return result


def _base_result(
    spec: CapabilitySpec,
    *,
    status: ProbeStatus,
    elapsed_ms: float,
    error_category: str | None,
    error_message: str | None,
) -> dict[str, object]:
    return {
        "capability_name": spec.capability_name,
        "api_name": spec.api_name,
        "status": status.value,
        "error_category": error_category,
        "error_message_sanitized": error_message,
        "elapsed_ms": elapsed_ms,
        "row_count": 0,
        "returned_fields": [],
        "has_source_time": False,
        "detected_time_fields": [],
        "price_fields": [],
        "volume_fields": [],
        "turnover_fields": [],
        "sample_symbol": spec.sample_symbol,
        "notes": spec.notes,
        "official_document_url": spec.document_url,
    }


def _extract_columns(table: object) -> list[str]:
    columns = getattr(table, "columns", None)
    if columns is None:
        raise InvalidProbeResponseError("response is not a DataFrame-like table")
    try:
        extracted = [str(column) for column in columns]
    except TypeError as error:
        raise InvalidProbeResponseError("response columns are not iterable") from error
    if len(extracted) != len(set(extracted)):
        raise InvalidProbeResponseError("response contains duplicate field names")
    return extracted


def _extract_inspection_records(table: object) -> list[dict[str, object]]:
    try:
        head = cast(Any, table).head(MAX_INSPECTION_ROWS)
        raw_records = head.to_dict(orient="records")
    except Exception as error:
        raise InvalidProbeResponseError(
            "response cannot produce metadata inspection records"
        ) from error
    if not isinstance(raw_records, list):
        raise InvalidProbeResponseError("response record conversion is invalid")

    records: list[dict[str, object]] = []
    for raw_record in raw_records:
        if not isinstance(raw_record, dict):
            raise InvalidProbeResponseError("response contains a non-object row")
        records.append({str(key): value for key, value in raw_record.items()})
    return records


def _unit_for_field(spec: CapabilitySpec, field: str) -> str:
    if field in spec.price_fields:
        if spec.capability_name.startswith("index_"):
            return "index_point"
        return "CNY_per_security"
    if spec.official_units is None:
        return "unknown_requires_verification"
    return spec.official_units.get(field, "unknown_requires_verification")


def _detect_symbol_formats(records: Sequence[Mapping[str, object]]) -> list[str]:
    formats: set[str] = set()
    for record in records:
        for field in ("ts_code", "code", "symbol"):
            value = record.get(field)
            if value is None or _is_missing(value):
                continue
            text = str(value)
            if re.fullmatch(r"\d{6}\.SH", text):
                formats.add("NNNNNN.SH")
            elif re.fullmatch(r"\d{6}\.SZ", text):
                formats.add("NNNNNN.SZ")
            elif re.fullmatch(r"\d{6}", text):
                formats.add("NNNNNN")
            else:
                formats.add("unknown_requires_verification")
    return sorted(formats)


def _detect_time_formats(
    records: Sequence[Mapping[str, object]], time_fields: Sequence[str]
) -> dict[str, str]:
    formats: dict[str, str] = {}
    for field in time_fields:
        observed: set[str] = set()
        for record in records:
            value = record.get(field)
            if value is None or _is_missing(value):
                continue
            observed.add(_time_format_name(str(value)))
        if not observed:
            formats[field] = "no_sample_value"
        elif len(observed) == 1:
            formats[field] = next(iter(observed))
        else:
            formats[field] = "mixed:" + ",".join(sorted(observed))
    return formats


def _time_format_name(value: str) -> str:
    if re.fullmatch(r"\d{8}", value):
        return "YYYYMMDD"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return "YYYY-MM-DD"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value):
        return "YYYY-MM-DD HH:MM:SS"
    if re.fullmatch(r"\d{2}:\d{2}:\d{2}", value):
        return "HH:MM:SS"
    return "unknown_requires_verification"


def _detect_sorting(
    records: Sequence[Mapping[str, object]], time_fields: Sequence[str]
) -> str:
    if not time_fields:
        return "not_applicable_no_time_field"
    field = time_fields[0]
    values = [
        str(record[field])
        for record in records
        if field in record and not _is_missing(record[field])
    ]
    if len(values) < 2:
        return "not_observable_from_sample"
    ascending = all(left <= right for left, right in pairwise(values))
    descending = all(left >= right for left, right in pairwise(values))
    if ascending and descending:
        return f"constant_by_{field}_in_inspected_rows"
    if ascending:
        return f"ascending_by_{field}_in_inspected_rows"
    if descending:
        return f"descending_by_{field}_in_inspected_rows"
    return f"mixed_by_{field}_in_inspected_rows"


def _detect_missing_values(
    records: Sequence[Mapping[str, object]],
) -> dict[str, list[str]]:
    missing: dict[str, set[str]] = {}
    for record in records:
        for field, value in record.items():
            representation = _missing_representation(value)
            if representation is not None:
                missing.setdefault(field, set()).add(representation)
    return {field: sorted(values) for field, values in sorted(missing.items())}


def _is_missing(value: object) -> bool:
    return _missing_representation(value) is not None


def _missing_representation(value: object) -> str | None:
    if value is None:
        return "null"
    if isinstance(value, float) and math.isnan(value):
        return "NaN"
    try:
        unequal = cast(Any, value) != cast(Any, value)
        if isinstance(unequal, bool) and unequal:
            return "NaN_like"
    except (TypeError, ValueError):
        return None
    return None


def _join_notes(*notes: str) -> str:
    return "; ".join(note for note in notes if note)


def run_probe(
    client: object,
    token: SecretStr,
    parameters: ProbeParameters,
    *,
    sdk_version: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.perf_counter,
) -> dict[str, object]:
    started_at = now()
    specs = build_capability_specs(parameters)
    results: list[dict[str, object]] = []
    for index, spec in enumerate(specs):
        results.append(
            probe_capability(client, spec, token, monotonic=monotonic)
        )
        if index < len(specs) - 1:
            sleep(REQUEST_PAUSE_SECONDS)
    completed_at = now()
    return build_report(
        results,
        parameters,
        sdk_version=sdk_version,
        started_at=started_at,
        completed_at=completed_at,
    )


def build_report(
    results: Sequence[Mapping[str, object]],
    parameters: ProbeParameters,
    *,
    sdk_version: str,
    started_at: datetime,
    completed_at: datetime,
) -> dict[str, object]:
    success_capabilities = _capabilities_with_status(results, ProbeStatus.SUCCESS)
    permission_limited = _capabilities_with_status(
        results, ProbeStatus.PERMISSION_DENIED
    )
    token_valid = _infer_token_validity(results)
    fixed_time_status = _fixed_time_requirement_status(results)
    payment_status = _payment_requirement_status(results)

    return {
        "schema_version": 1,
        "probe_name": "tushare_free_tier_capabilities",
        "sdk_version": sdk_version,
        "python_version": sys.version.split()[0],
        "started_at": _isoformat_utc(started_at),
        "completed_at": _isoformat_utc(completed_at),
        "parameters": {
            "sh_stock": parameters.sh_stock,
            "sz_stock": parameters.sz_stock,
            "sh_etf": parameters.sh_etf,
            "index": parameters.index,
            "trade_date": parameters.trade_date,
        },
        "token_valid": token_valid,
        "capabilities": [dict(result) for result in results],
        "assessment": {
            "free_account_available_capabilities": success_capabilities,
            "permission_or_points_limited_capabilities": permission_limited,
            "fixed_time_a_share_requirement": fixed_time_status,
            "payment_required_to_continue": payment_status,
            "market_hours_verification_required": [
                "09:15:30 auction-start behavior",
                "09:20:30 auction intermediate behavior",
                "09:25:30 official open-price availability",
                "09:26-09:29 stk_auction availability window",
                "ETF auction-row availability from the same stk_auction API",
                "09:35:00 continuous-trading freshness",
                "11:35:00 midday freshness",
                "15:05:00 close completeness",
            ],
            "decision": (
                "This probe does not accept a provider or change ADR-0004 status."
            ),
        },
    }


def _capabilities_with_status(
    results: Sequence[Mapping[str, object]], status: ProbeStatus
) -> list[str]:
    return [
        str(result["capability_name"])
        for result in results
        if result.get("status") == status.value
    ]


def _infer_token_validity(
    results: Sequence[Mapping[str, object]],
) -> bool | str:
    accepted_statuses = {
        ProbeStatus.SUCCESS.value,
        ProbeStatus.PERMISSION_DENIED.value,
        ProbeStatus.RATE_LIMITED.value,
    }
    statuses = [str(result.get("status")) for result in results]
    if any(status in accepted_statuses for status in statuses):
        return True
    if statuses and all(
        status == ProbeStatus.AUTHENTICATION_FAILED.value for status in statuses
    ):
        return False
    return "unknown"


def _fixed_time_requirement_status(
    results: Sequence[Mapping[str, object]],
) -> str:
    by_capability = {
        str(result["capability_name"]): str(result["status"])
        for result in results
    }
    required = (
        "stock_realtime_snapshot",
        "stock_realtime_minute",
        "etf_realtime_snapshot",
        "opening_auction",
    )
    statuses = [by_capability.get(capability) for capability in required]
    if all(status == ProbeStatus.SUCCESS.value for status in statuses):
        return "capabilities_available_but_market_hours_validation_required"
    if any(status == ProbeStatus.PERMISSION_DENIED.value for status in statuses):
        return "not_demonstrated_due_to_permission_or_points_limits"
    return "not_demonstrated"


def _payment_requirement_status(
    results: Sequence[Mapping[str, object]],
) -> str:
    required_names = {
        "stock_realtime_snapshot",
        "stock_realtime_minute",
        "etf_realtime_snapshot",
        "opening_auction",
    }
    required_results = [
        result
        for result in results
        if result.get("capability_name") in required_names
    ]
    if required_results and all(
        result.get("status") == ProbeStatus.SUCCESS.value
        for result in required_results
    ):
        return "not_required_for_the_probed_calls_but_future_cost_not_assessed"
    if any(
        result.get("status") == ProbeStatus.PERMISSION_DENIED.value
        for result in required_results
    ):
        return "not_determined_permission_or_points_upgrade_needed_payment_not_proven"
    return "not_determined"


def _isoformat_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("probe timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def write_report(report: Mapping[str, object], project_root: Path) -> Path:
    report_path = project_root / REPORT_RELATIVE_PATH
    report_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    report_path.write_text(serialized + "\n", encoding="utf-8")
    return report_path


def print_summary(report: Mapping[str, object]) -> None:
    raw_capabilities = report.get("capabilities")
    capabilities = (
        cast(Sequence[Mapping[str, object]], raw_capabilities)
        if isinstance(raw_capabilities, list)
        else ()
    )
    success = sum(
        capability.get("status") == ProbeStatus.SUCCESS.value
        for capability in capabilities
    )
    permission_denied = sum(
        capability.get("status") == ProbeStatus.PERMISSION_DENIED.value
        for capability in capabilities
    )
    failed = len(capabilities) - success - permission_denied
    print("Tushare free-tier capability probe")
    print(f"success={success}")
    print(f"permission_denied={permission_denied}")
    print(f"failed={failed}")
    print(f"report={REPORT_RELATIVE_PATH.as_posix()}")


def _parse_arguments(argv: Sequence[str] | None) -> ProbeParameters:
    parser = argparse.ArgumentParser(
        description="Probe Tushare Pro free-tier metadata capabilities."
    )
    parser.add_argument("--sh-stock", default="600519.SH")
    parser.add_argument("--sz-stock", default="000001.SZ")
    parser.add_argument("--sh-etf", default="510300.SH")
    parser.add_argument("--index", default="000001.SH")
    parser.add_argument("--trade-date", default="20260724")
    namespace = parser.parse_args(argv)
    parameters = ProbeParameters(
        sh_stock=namespace.sh_stock,
        sz_stock=namespace.sz_stock,
        sh_etf=namespace.sh_etf,
        index=namespace.index,
        trade_date=namespace.trade_date,
    )
    _validate_parameters(parameters)
    return parameters


def _validate_parameters(parameters: ProbeParameters) -> None:
    for name, value in (
        ("sh_stock", parameters.sh_stock),
        ("sz_stock", parameters.sz_stock),
        ("sh_etf", parameters.sh_etf),
        ("index", parameters.index),
    ):
        if not STOCK_CODE_PATTERN.fullmatch(value):
            raise ProbeConfigurationError(
                f"{name} must use the six-digit .SH or .SZ Tushare code format"
            )
    if not parameters.sh_stock.endswith(".SH"):
        raise ProbeConfigurationError("sh_stock must use the .SH suffix")
    if not parameters.sz_stock.endswith(".SZ"):
        raise ProbeConfigurationError("sz_stock must use the .SZ suffix")
    if not parameters.sh_etf.endswith(".SH"):
        raise ProbeConfigurationError(
            "sh_etf must use the .SH suffix because rt_etf_k uses the Shanghai topic"
        )
    try:
        date.fromisoformat(
            f"{parameters.trade_date[:4]}-"
            f"{parameters.trade_date[4:6]}-"
            f"{parameters.trade_date[6:]}"
        )
    except ValueError as error:
        raise ProbeConfigurationError(
            "trade_date must be a valid YYYYMMDD date"
        ) from error


def _load_tushare_module() -> object:
    try:
        return importlib.import_module("tushare")
    except ImportError as error:
        raise ProbeConfigurationError(
            "Tushare SDK is not installed; install scripts/spikes/tushare/requirements.txt"
        ) from error


def main(
    argv: Sequence[str] | None = None,
    *,
    project_root: Path | None = None,
    tushare_module: object | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    root = project_root or PROJECT_ROOT
    try:
        parameters = _parse_arguments(argv)
        token = load_token_from_project_env(root)
        module = tushare_module or _load_tushare_module()
        pro_api = cast(Callable[[str], object], cast(Any, module).pro_api)
        client = pro_api(token.get_secret_value())
        report = run_probe(
            client,
            token,
            parameters,
            sdk_version=str(getattr(module, "__version__", "unknown")),
            sleep=sleep,
        )
        write_report(report, root)
        print_summary(report)
        return 2 if report["token_valid"] is False else 0
    except ProbeConfigurationError as error:
        print(f"Tushare probe configuration error: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 - top-level output must remain sanitized
        secrets: tuple[str, ...] = ()
        if "token" in locals():
            secrets = (token.get_secret_value(),)
        message = sanitize_error_message(error, secrets)
        print(f"Tushare probe failed: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
