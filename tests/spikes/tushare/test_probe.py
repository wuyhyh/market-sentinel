from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from pydantic import SecretStr

from scripts.spikes.tushare.probe import (
    CapabilitySpec,
    ProbeParameters,
    ProbeStatus,
    build_capability_specs,
    build_report,
    classify_error,
    load_token_from_project_env,
    main,
    print_summary,
    probe_capability,
    run_probe,
    sanitize_error_message,
    write_report,
)

TOKEN = "test-token-value-that-must-never-appear"
FIXED_NOW = datetime(2026, 7, 24, 7, 5, tzinfo=UTC)
PARAMETERS = ProbeParameters(
    sh_stock="600519.SH",
    sz_stock="000001.SZ",
    sh_etf="510300.SH",
    index="000001.SH",
    trade_date="20260724",
)


class FakeTable:
    def __init__(self, records: Sequence[Mapping[str, object]]) -> None:
        self._records = [dict(record) for record in records]
        self.columns = list(self._records[0]) if self._records else []

    def __len__(self) -> int:
        return len(self._records)

    def head(self, count: int) -> FakeTable:
        return FakeTable(self._records[:count])

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
    __version__ = "fake-1.0"

    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.pro_api_calls = 0
        self.received_token: str | None = None

    def pro_api(self, token: str) -> FakeClient:
        self.pro_api_calls += 1
        self.received_token = token
        return self.client

    def set_token(self, token: str) -> None:
        raise AssertionError(f"set_token must not be called: {token}")


def _write_test_env(project_root: Path, token: str = TOKEN) -> None:
    (project_root / ".env").write_text(
        f"APP_ENV=test\nTUSHARE_TOKEN={token}\n", encoding="utf-8"
    )


def _spec(api_name: str = "rt_k") -> CapabilitySpec:
    return CapabilitySpec(
        capability_name="test_capability",
        api_name=api_name,
        request_kwargs={"ts_code": "600519.SH"},
        sample_symbol="600519.SH",
        document_url="https://tushare.pro/document/test",
        price_fields=("open", "close"),
        volume_fields=("vol",),
        turnover_fields=("amount",),
        official_units={"vol": "share", "amount": "CNY"},
    )


def _fixed_clock() -> Callable[[], float]:
    values = iter((10.0, 10.125))
    return lambda: next(values)


def test_missing_token_fails_before_client_initialization(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = FakeTushareModule(FakeClient({}))

    exit_code = main(
        [],
        project_root=tmp_path,
        tushare_module=module,
        sleep=lambda _: None,
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert module.pro_api_calls == 0
    assert "TUSHARE_TOKEN is missing" in captured.err
    assert not (tmp_path / "data").exists()


def test_token_is_never_exposed_in_output_or_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_test_env(tmp_path)
    secret_error = RuntimeError(
        f"token={TOKEN} phone=13800138000 authorization=Bearer {TOKEN}"
    )
    module = FakeTushareModule(
        FakeClient(
            {
                spec.api_name: secret_error
                for spec in build_capability_specs(PARAMETERS)
            }
        )
    )

    exit_code = main(
        [],
        project_root=tmp_path,
        tushare_module=module,
        sleep=lambda _: None,
    )

    captured = capsys.readouterr()
    report_text = (
        tmp_path / "data/spikes/tushare/free-tier-capabilities.json"
    ).read_text(encoding="utf-8")
    assert exit_code == 0
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err
    assert TOKEN not in report_text
    assert "13800138000" not in report_text
    assert "[redacted-secret]" in report_text
    assert "[redacted-phone]" in report_text
    assert module.received_token == TOKEN


def test_authentication_failure_marks_token_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_test_env(tmp_path)
    module = FakeTushareModule(
        FakeClient(
            {
                spec.api_name: RuntimeError("抱歉，您输入的TOKEN无效！")
                for spec in build_capability_specs(PARAMETERS)
            }
        )
    )

    exit_code = main(
        [],
        project_root=tmp_path,
        tushare_module=module,
        sleep=lambda _: None,
    )

    captured = capsys.readouterr()
    report = json.loads(
        (
            tmp_path / "data/spikes/tushare/free-tier-capabilities.json"
        ).read_text(encoding="utf-8")
    )
    assert exit_code == 2
    assert report["token_valid"] is False
    assert {
        capability["status"] for capability in report["capabilities"]
    } == {"authentication_failed"}
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err


def test_successful_interface_extracts_metadata_only() -> None:
    table = FakeTable(
        [
            {
                "ts_code": "600519.SH",
                "trade_time": "2026-07-24 09:35:00",
                "open": 1400.0,
                "close": 1401.0,
                "vol": 100,
                "amount": 140100.0,
            }
        ]
    )

    result = probe_capability(
        FakeClient({"rt_k": table}),
        _spec(),
        SecretStr(TOKEN),
        monotonic=_fixed_clock(),
    )

    assert result["status"] == "success"
    assert result["row_count"] == 1
    assert result["elapsed_ms"] == 125.0
    assert result["returned_fields"] == [
        "amount",
        "close",
        "open",
        "trade_time",
        "ts_code",
        "vol",
    ]
    assert result["has_source_time"] is True
    assert result["detected_time_fields"] == ["trade_time"]
    assert result["price_fields"] == ["open", "close"]
    assert result["volume_fields"] == ["vol"]
    assert result["turnover_fields"] == ["amount"]
    assert result["symbol_formats"] == ["NNNNNN.SH"]
    assert result["time_field_formats"] == {
        "trade_time": "YYYY-MM-DD HH:MM:SS"
    }
    assert "1401" not in json.dumps(result)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            RuntimeError(
                "抱歉，您没有接口(trade_cal)访问权限，权限的具体详情访问……"
            ),
            ProbeStatus.PERMISSION_DENIED,
        ),
        (
            RuntimeError("当前账户权限不足，无法访问该接口"),
            ProbeStatus.PERMISSION_DENIED,
        ),
        (
            RuntimeError("您的积分不足，无法调取本接口"),
            ProbeStatus.PERMISSION_DENIED,
        ),
        (
            RuntimeError("抱歉，您输入的TOKEN无效！"),
            ProbeStatus.AUTHENTICATION_FAILED,
        ),
        (
            RuntimeError("每分钟最多访问该接口1次"),
            ProbeStatus.RATE_LIMITED,
        ),
        (TimeoutError("request timed out"), ProbeStatus.TIMEOUT),
        (
            RuntimeError("参数错误：trade_date格式不正确"),
            ProbeStatus.INVALID_REQUEST,
        ),
    ],
)
def test_known_errors_are_classified(
    error: BaseException, expected: ProbeStatus
) -> None:
    result = probe_capability(
        FakeClient({"rt_k": error}),
        _spec(),
        SecretStr(TOKEN),
        monotonic=_fixed_clock(),
    )

    assert classify_error(error) is expected
    assert result["status"] == expected.value
    assert result["error_category"] == expected.value


def test_unknown_error_is_sanitized() -> None:
    error = RuntimeError(
        f"unexpected token={TOKEN}; user=private-user; phone 13912345678; "
        "mail person@example.com"
    )

    result = probe_capability(
        FakeClient({"rt_k": error}),
        _spec(),
        SecretStr(TOKEN),
        monotonic=_fixed_clock(),
    )
    serialized = json.dumps(result)

    assert result["status"] == ProbeStatus.UNEXPECTED_ERROR.value
    assert TOKEN not in serialized
    assert "private-user" not in serialized
    assert "13912345678" not in serialized
    assert "person@example.com" not in serialized
    assert "[redacted-secret]" in str(result["error_message_sanitized"])
    assert "[redacted-account]" in str(result["error_message_sanitized"])
    assert sanitize_error_message(error, (TOKEN,))


def test_permission_denied_is_not_failed_and_is_listed_in_assessment(
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec = CapabilitySpec(
        capability_name="trading_calendar",
        api_name="trade_cal",
        request_kwargs={"exchange": "SSE"},
        sample_symbol=None,
        document_url="https://tushare.pro/document/2?doc_id=26",
    )
    result = probe_capability(
        FakeClient(
            {
                "trade_cal": RuntimeError(
                    "抱歉，您没有接口(trade_cal)访问权限，权限的具体详情访问……"
                )
            }
        ),
        spec,
        SecretStr(TOKEN),
        monotonic=_fixed_clock(),
    )
    report = build_report(
        [result],
        PARAMETERS,
        sdk_version="fake-1.0",
        started_at=FIXED_NOW,
        completed_at=FIXED_NOW,
    )

    print_summary(report)

    captured = capsys.readouterr()
    assessment = cast(Mapping[str, object], report["assessment"])
    assert "permission_denied=1" in captured.out
    assert "failed=0" in captured.out
    assert assessment["permission_or_points_limited_capabilities"] == [
        "trading_calendar"
    ]


def test_report_is_written_only_under_ignored_data_directory(tmp_path: Path) -> None:
    report = build_report(
        [],
        PARAMETERS,
        sdk_version="fake-1.0",
        started_at=FIXED_NOW,
        completed_at=FIXED_NOW,
    )

    report_path = write_report(report, tmp_path)

    assert report_path == (
        tmp_path / "data/spikes/tushare/free-tier-capabilities.json"
    )
    assert json.loads(report_path.read_text(encoding="utf-8"))["schema_version"] == 1


def test_large_dataframe_rows_are_not_saved() -> None:
    records = [
        {
            "ts_code": "600519.SH",
            "trade_time": f"2026-07-24 09:{minute:02d}:00",
            "close": float(minute),
            "raw_marker": f"private-row-{minute}",
        }
        for minute in range(60)
    ]
    result = probe_capability(
        FakeClient({"rt_k": FakeTable(records)}),
        _spec(),
        SecretStr(TOKEN),
        monotonic=_fixed_clock(),
    )
    serialized = json.dumps(result)

    assert result["row_count"] == 60
    assert result["inspection_scope"] == "first_20_rows_metadata_only"
    assert "private-row-" not in serialized
    assert '"records"' not in serialized


def test_capability_output_order_is_stable() -> None:
    client = FakeClient({})
    report = run_probe(
        client,
        SecretStr(TOKEN),
        PARAMETERS,
        sdk_version="fake-1.0",
        now=lambda: FIXED_NOW,
        sleep=lambda _: None,
        monotonic=lambda: 10.0,
    )

    capabilities = cast(list[dict[str, object]], report["capabilities"])
    capability_names = [
        str(result["capability_name"]) for result in capabilities
    ]
    expected_names = [
        spec.capability_name for spec in build_capability_specs(PARAMETERS)
    ]
    assert capability_names == expected_names
    assert [api_name for api_name, _ in client.calls] == [
        spec.api_name for spec in build_capability_specs(PARAMETERS)
    ]
    assert len(client.calls) == len({api_name for api_name, _ in client.calls})


def test_load_token_returns_secretstr_without_environment_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TUSHARE_TOKEN", "must-not-be-used")
    _write_test_env(tmp_path)

    token = load_token_from_project_env(tmp_path)

    assert isinstance(token, SecretStr)
    assert str(token) == "**********"
    assert token.get_secret_value() == TOKEN
