from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from market_sentinel.domain.models import TradingMarket
from market_sentinel.domain.security_data import (
    Currency,
    DataCompleteness,
    ListStatus,
    SecurityCategory,
    SecurityExchange,
    SecurityMasterBatch,
    SecurityMasterRecord,
)
from market_sentinel.market_data.security_master_cache import (
    SecurityMasterCache,
    SecurityMasterCacheError,
)

NOW = datetime(2026, 7, 24, 8, 0, tzinfo=UTC)
SYMBOLS = ("000333.SZ", "510300.SH", "600183.SH")


def make_batch() -> SecurityMasterBatch:
    records = tuple(
        SecurityMasterRecord(
            symbol=symbol,
            provider_symbol=symbol,
            name=f"fixture-{symbol}",
            market=TradingMarket.A_SHARE,
            exchange=(
                SecurityExchange.XSHG
                if symbol.endswith(".SH")
                else SecurityExchange.XSHE
            ),
            security_type=SecurityCategory.STOCK,
            currency=Currency.CNY,
            list_status=ListStatus.LISTED,
            list_date=None,
            source="tushare",
            received_at=NOW,
        )
        for symbol in ("000333.SZ", "600183.SH")
    )
    return SecurityMasterBatch(
        requested_symbols=SYMBOLS,
        records=records,
        unsupported_symbols=("510300.SH",),
        completeness=DataCompleteness.PARTIAL,
        source="tushare",
        requested_at=NOW,
        completed_at=NOW,
    )


def test_cache_round_trip_is_stable_and_preserves_required_metadata(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "security-master.json"
    cache = SecurityMasterCache(cache_path)

    document = cache.write_atomic(make_batch(), fetched_at=NOW)
    entry = cache.load(
        expected_symbols=tuple(reversed(SYMBOLS)),
        now=NOW + timedelta(days=1),
        max_age=timedelta(days=7),
    )

    assert document.schema_version == 1
    assert entry.document.received_at == NOW
    assert entry.batch == make_batch()
    assert entry.age_seconds == 86400
    assert entry.is_stale is False
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert list(payload) == [
        "schema_version",
        "source",
        "fetched_at",
        "received_at",
        "requested_symbols",
        "records",
        "missing_symbols",
        "unsupported_symbols",
        "invalid_symbols",
        "provider_errors",
        "completeness",
        "requested_at",
        "completed_at",
    ]
    assert "token" not in cache_path.read_text(encoding="utf-8").lower()


@pytest.mark.parametrize(
    ("contents", "expected_code"),
    [
        ("not-json", "invalid_json"),
        ('{"schema_version": 999}', "unsupported_schema"),
        ("[]", "invalid_cache"),
    ],
)
def test_cache_rejects_invalid_json_and_unsupported_schema(
    contents: str,
    expected_code: str,
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "security-master.json"
    cache_path.write_text(contents, encoding="utf-8")

    with pytest.raises(SecurityMasterCacheError) as captured:
        SecurityMasterCache(cache_path).load(
            expected_symbols=SYMBOLS,
            now=NOW,
            max_age=timedelta(days=7),
        )

    assert captured.value.code == expected_code


def test_cache_rejects_watchlist_mismatch(tmp_path: Path) -> None:
    cache = SecurityMasterCache(tmp_path / "security-master.json")
    cache.write_atomic(make_batch(), fetched_at=NOW)

    with pytest.raises(SecurityMasterCacheError) as captured:
        cache.load(
            expected_symbols=("600183.SH",),
            now=NOW,
            max_age=timedelta(days=7),
        )

    assert captured.value.code == "watchlist_mismatch"


def test_atomic_write_failure_preserves_existing_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_path = tmp_path / "security-master.json"
    cache = SecurityMasterCache(cache_path)
    cache.write_atomic(make_batch(), fetched_at=NOW - timedelta(days=1))
    original = cache_path.read_bytes()

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("simulated atomic replacement failure")

    monkeypatch.setattr(
        "market_sentinel.market_data.security_master_cache.os.replace",
        fail_replace,
    )

    with pytest.raises(SecurityMasterCacheError) as captured:
        cache.write_atomic(make_batch(), fetched_at=NOW)

    assert captured.value.code == "cache_write_failed"
    assert cache_path.read_bytes() == original
    assert not tuple(tmp_path.glob(".security-master.json.*.tmp"))


def test_atomic_write_successfully_replaces_existing_cache(tmp_path: Path) -> None:
    cache_path = tmp_path / "security-master.json"
    cache = SecurityMasterCache(cache_path)
    cache.write_atomic(make_batch(), fetched_at=NOW - timedelta(days=1))
    original = cache_path.read_bytes()

    cache.write_atomic(make_batch(), fetched_at=NOW)

    assert cache_path.read_bytes() != original
    entry = cache.load(
        expected_symbols=SYMBOLS,
        now=NOW,
        max_age=timedelta(days=7),
    )
    assert entry.document.fetched_at == NOW
    assert entry.age_seconds == 0


def test_cache_marks_age_over_maximum_as_stale(tmp_path: Path) -> None:
    cache = SecurityMasterCache(tmp_path / "security-master.json")
    cache.write_atomic(make_batch(), fetched_at=NOW - timedelta(days=8))

    entry = cache.load(
        expected_symbols=SYMBOLS,
        now=NOW,
        max_age=timedelta(days=7),
    )

    assert entry.is_stale is True
    assert entry.age_seconds == 8 * 86400
