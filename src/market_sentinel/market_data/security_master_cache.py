from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from market_sentinel.domain.security_data import (
    DataCompleteness,
    ProviderError,
    SecurityMasterBatch,
    SecurityMasterRecord,
)

SECURITY_MASTER_CACHE_SCHEMA_VERSION = 1


class SecurityMasterCacheError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SecurityMasterCacheDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    source: str
    fetched_at: datetime
    received_at: datetime
    requested_symbols: tuple[str, ...]
    records: tuple[SecurityMasterRecord, ...]
    missing_symbols: tuple[str, ...] = ()
    unsupported_symbols: tuple[str, ...] = ()
    invalid_symbols: tuple[str, ...] = ()
    provider_errors: tuple[ProviderError, ...] = ()
    completeness: DataCompleteness
    requested_at: datetime
    completed_at: datetime

    @field_validator("fetched_at", "received_at", "requested_at", "completed_at")
    @classmethod
    def validate_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("cache timestamps must be timezone-aware")
        return value

    @classmethod
    def from_batch(
        cls,
        batch: SecurityMasterBatch,
        *,
        fetched_at: datetime,
    ) -> SecurityMasterCacheDocument:
        if batch.completeness is DataCompleteness.FAILED:
            raise ValueError("failed security-master batches must not be cached")
        return cls(
            source=batch.source,
            fetched_at=fetched_at,
            received_at=batch.completed_at,
            requested_symbols=batch.requested_symbols,
            records=batch.records,
            missing_symbols=batch.missing_symbols,
            unsupported_symbols=batch.unsupported_symbols,
            invalid_symbols=batch.invalid_symbols,
            provider_errors=batch.provider_errors,
            completeness=batch.completeness,
            requested_at=batch.requested_at,
            completed_at=batch.completed_at,
        )

    def to_batch(self) -> SecurityMasterBatch:
        return SecurityMasterBatch(
            requested_symbols=self.requested_symbols,
            records=self.records,
            missing_symbols=self.missing_symbols,
            unsupported_symbols=self.unsupported_symbols,
            invalid_symbols=self.invalid_symbols,
            provider_errors=self.provider_errors,
            completeness=self.completeness,
            source=self.source,
            requested_at=self.requested_at,
            completed_at=self.completed_at,
        )


class SecurityMasterCacheEntry(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    document: SecurityMasterCacheDocument
    batch: SecurityMasterBatch
    age_seconds: int
    is_stale: bool


class SecurityMasterCache:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(
        self,
        *,
        expected_symbols: tuple[str, ...],
        now: datetime,
        max_age: timedelta,
    ) -> SecurityMasterCacheEntry:
        if not self.path.exists():
            raise SecurityMasterCacheError(
                "cache_miss", f"security-master cache does not exist: {self.path}"
            )
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SecurityMasterCacheError(
                "invalid_json", "security-master cache is not valid JSON"
            ) from exc
        except OSError as exc:
            raise SecurityMasterCacheError(
                "cache_unreadable", "security-master cache cannot be read"
            ) from exc

        if not isinstance(payload, dict):
            raise SecurityMasterCacheError(
                "invalid_cache", "security-master cache root must be an object"
            )
        if payload.get("schema_version") != SECURITY_MASTER_CACHE_SCHEMA_VERSION:
            raise SecurityMasterCacheError(
                "unsupported_schema",
                "security-master cache schema version is not supported",
            )
        try:
            document = SecurityMasterCacheDocument.model_validate(payload)
            batch = document.to_batch()
        except (TypeError, ValueError) as exc:
            raise SecurityMasterCacheError(
                "invalid_cache", "security-master cache content is invalid"
            ) from exc
        if document.completeness is DataCompleteness.FAILED:
            raise SecurityMasterCacheError(
                "invalid_cache", "failed security-master results cannot be used as cache"
            )

        normalized_expected = tuple(sorted(set(expected_symbols)))
        if document.requested_symbols != normalized_expected:
            raise SecurityMasterCacheError(
                "watchlist_mismatch",
                "security-master cache does not match the current watchlist",
            )

        now_utc = _as_utc(now)
        fetched_at_utc = _as_utc(document.fetched_at)
        age = now_utc - fetched_at_utc
        if age.total_seconds() < 0:
            raise SecurityMasterCacheError(
                "invalid_cache", "security-master cache fetched_at is in the future"
            )
        age_seconds = int(age.total_seconds())
        return SecurityMasterCacheEntry(
            document=document,
            batch=batch,
            age_seconds=age_seconds,
            is_stale=age > max_age,
        )

    def write_atomic(
        self,
        batch: SecurityMasterBatch,
        *,
        fetched_at: datetime,
    ) -> SecurityMasterCacheDocument:
        document = SecurityMasterCacheDocument.from_batch(
            batch, fetched_at=fetched_at
        )
        serialized = document.model_dump_json(indent=2)
        SecurityMasterCacheDocument.model_validate_json(serialized)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(serialized)
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self.path)
        except OSError as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise SecurityMasterCacheError(
                "cache_write_failed", "security-master cache could not be written"
            ) from exc
        return document


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SecurityMasterCacheError(
            "invalid_cache", "security-master cache timestamps must be timezone-aware"
        )
    return value.astimezone(UTC)
