from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from market_sentinel.domain.watchlist import SecurityRole, WatchPriority
from market_sentinel.watchlist import WatchlistConfigurationError, WatchlistLoader

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _security(
    symbol: str = "600001.SH",
    *,
    name: str = "示例证券",
    exchange: str = "SH",
    security_type: str = "stock",
    enabled: bool = True,
    roles: list[str] | None = None,
    priority: str = "normal",
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "name": name,
        "market": "a_share",
        "exchange": exchange,
        "security_type": security_type,
        "enabled": enabled,
        "roles": ["watch"] if roles is None else roles,
        "priority": priority,
    }


def _write_config(path: Path, securities: list[dict[str, object]]) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "declared_count": len(securities),
                "securities": securities,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _load_error(path: Path) -> WatchlistConfigurationError:
    with pytest.raises(WatchlistConfigurationError) as exc_info:
        WatchlistLoader().load(path)
    return exc_info.value


def test_complete_valid_watchlist_is_normalized_stably(tmp_path: Path) -> None:
    config_path = tmp_path / "watchlist.yaml"
    _write_config(
        config_path,
        [
            _security("300001.SZ", exchange="SZ"),
            _security(
                "510001.SH",
                name="示例ETF",
                security_type="etf",
                roles=["watch", "holding"],
                priority="critical",
            ),
        ],
    )

    watchlist = WatchlistLoader().load(config_path)

    assert [security.symbol for security in watchlist.securities] == [
        "300001.SZ",
        "510001.SH",
    ]
    assert watchlist.securities[1].roles == (
        SecurityRole.HOLDING,
        SecurityRole.WATCH,
    )
    assert watchlist.securities[1].priority is WatchPriority.CRITICAL


def test_bare_six_digit_symbols_and_defaults_are_normalized(tmp_path: Path) -> None:
    config_path = tmp_path / "watchlist.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "securities": [
                    {
                        "symbol": "300001",
                        "name": "默认观察证券",
                        "market": "a_share",
                        "exchange": "SZ",
                        "security_type": "stock",
                    }
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    security = WatchlistLoader().load(config_path).securities[0]

    assert security.symbol == "300001.SZ"
    assert security.enabled is True
    assert security.roles == (SecurityRole.WATCH,)
    assert security.priority is WatchPriority.NORMAL


def test_duplicate_security_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "watchlist.yaml"
    _write_config(
        config_path,
        [
            _security("600001.SH", name="名称一"),
            _security("600001.SH", name="名称二"),
        ],
    )

    error = _load_error(config_path)

    assert "duplicate symbols" in str(error)


@pytest.mark.parametrize(
    ("changes", "expected_message"),
    [
        ({"symbol": "60001.SH"}, "String should match pattern"),
        (
            {"symbol": "600001.SZ", "exchange": "SZ"},
            "symbol prefix must match exchange",
        ),
        ({"symbol": "600001.SH", "exchange": "SZ"}, "symbol suffix must match exchange"),
        (
            {"security_type": "bond"},
            "Input should be 'stock', 'etf' or 'index'",
        ),
    ],
)
def test_invalid_security_fields_are_rejected(
    changes: dict[str, Any],
    expected_message: str,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "watchlist.yaml"
    security = _security()
    security.update(changes)
    _write_config(config_path, [security])

    error = _load_error(config_path)

    assert expected_message in str(error)


@pytest.mark.parametrize(
    ("enabled", "priority", "expected_message"),
    [
        (True, "normal", "holding securities must have critical priority"),
        (False, "critical", "holding securities must be enabled"),
    ],
)
def test_holding_invariants_are_enforced(
    enabled: bool,
    priority: str,
    expected_message: str,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "watchlist.yaml"
    _write_config(
        config_path,
        [
            _security(
                enabled=enabled,
                roles=["holding", "watch"],
                priority=priority,
            )
        ],
    )

    error = _load_error(config_path)

    assert expected_message in str(error)


def test_yaml_error_is_explicit(tmp_path: Path) -> None:
    config_path = tmp_path / "watchlist.yaml"
    config_path.write_text("securities: [\n", encoding="utf-8")

    error = _load_error(config_path)

    assert error.issues[0].code == "yaml_error"


def test_missing_file_error_is_explicit(tmp_path: Path) -> None:
    error = _load_error(tmp_path / "missing.yaml")

    assert error.issues[0].code == "file_not_found"


def test_empty_watchlist_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "watchlist.yaml"
    _write_config(config_path, [])

    error = _load_error(config_path)

    assert "securities must not be empty" in str(error)


def test_declared_count_mismatch_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "watchlist.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "declared_count": 94,
                "securities": [
                    _security(f"{600000 + index:06d}.SH")
                    for index in range(93)
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    error = _load_error(config_path)

    assert "declared_count does not match" in str(error)


def test_input_order_does_not_change_normalized_output(tmp_path: Path) -> None:
    first_path = tmp_path / "first.yaml"
    second_path = tmp_path / "second.yaml"
    securities = [
        _security("600001.SH"),
        _security("159001.SZ", exchange="SZ", security_type="etf"),
    ]
    _write_config(first_path, securities)
    _write_config(second_path, list(reversed(securities)))

    first = WatchlistLoader().load(first_path)
    second = WatchlistLoader().load(second_path)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_example_watchlist_is_valid() -> None:
    watchlist = WatchlistLoader().load(
        PROJECT_ROOT / "config/watchlist.example.yaml"
    )

    assert len(watchlist.securities) == 2
    assert all(
        security.roles == (SecurityRole.WATCH,)
        for security in watchlist.securities
    )


def test_personal_watchlist_path_is_ignored_and_not_tracked() -> None:
    ignored = subprocess.run(
        [
            "git",
            "check-ignore",
            "--no-index",
            "--quiet",
            "config/watchlist.yaml",
        ],
        cwd=PROJECT_ROOT,
        check=False,
    )
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "config/watchlist.yaml"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert ignored.returncode == 0
    assert tracked.returncode != 0
