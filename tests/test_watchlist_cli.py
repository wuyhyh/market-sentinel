from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from market_sentinel import cli


def _write_config(path: Path, *, reversed_order: bool = False) -> None:
    securities: list[dict[str, object]] = [
        {
            "symbol": "600001.SH",
            "name": "示例股票",
            "market": "a_share",
            "exchange": "SH",
            "security_type": "stock",
            "enabled": True,
            "roles": ["watch"],
            "priority": "normal",
        },
        {
            "symbol": "159001.SZ",
            "name": "示例ETF",
            "market": "a_share",
            "exchange": "SZ",
            "security_type": "etf",
            "enabled": True,
            "roles": ["holding", "watch"],
            "priority": "critical",
        },
    ]
    if reversed_order:
        securities.reverse()
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "declared_count": 2,
                "securities": securities,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_watchlist_validate_success_outputs_machine_readable_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "watchlist.yaml"
    _write_config(config_path)

    exit_code = cli.main(
        ["watchlist", "validate", "--config", str(config_path)]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload == {
        "status": "valid",
        "config_path": config_path.as_posix(),
        "total_count": 2,
        "enabled_count": 2,
        "stock_count": 1,
        "etf_count": 1,
        "holding_count": 1,
        "critical_count": 1,
        "exchanges": {"SH": 1, "SZ": 1},
        "validation_errors": [],
    }


def test_watchlist_validate_failure_is_json_and_nonzero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "missing.yaml"

    exit_code = cli.main(
        ["watchlist", "validate", "--config", str(config_path)]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["status"] == "invalid"
    assert payload["total_count"] is None
    assert payload["validation_errors"] == [
        {
            "code": "file_not_found",
            "path": "$",
            "message": "watchlist configuration does not exist",
        }
    ]


def test_watchlist_summary_does_not_expose_unconfigured_financial_data(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "watchlist.yaml"
    _write_config(config_path)

    exit_code = cli.main(
        ["watchlist", "summary", "--config", str(config_path)]
    )

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert exit_code == 0
    assert payload["total_count"] == 2
    assert "market_value" not in output
    assert "cost" not in output
    assert "token" not in output.casefold()
    assert "notes" not in output


def test_watchlist_cli_output_is_stable_across_input_order(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first_path = tmp_path / "first.yaml"
    second_path = tmp_path / "second.yaml"
    _write_config(first_path)
    _write_config(second_path, reversed_order=True)

    assert cli.main(["watchlist", "summary", "--config", str(first_path)]) == 0
    first_payload = json.loads(capsys.readouterr().out)
    assert cli.main(["watchlist", "summary", "--config", str(second_path)]) == 0
    second_payload = json.loads(capsys.readouterr().out)

    first_payload.pop("config_path")
    second_payload.pop("config_path")
    assert first_payload == second_payload

