import argparse
import json

import pytest

from market_sentinel import cli
from market_sentinel.domain.models import JobRunStatus, MarketPhase


class StubReportService:
    def __init__(
        self,
        *,
        status: JobRunStatus | None = None,
        error: Exception | None = None,
    ) -> None:
        self._status = status
        self._error = error
        self.phases: list[MarketPhase] = []

    async def run(self, phase: MarketPhase) -> JobRunStatus:
        self.phases.append(phase)
        if self._error is not None:
            raise self._error
        assert self._status is not None
        return self._status


@pytest.mark.parametrize(
    "status",
    [
        JobRunStatus.COMPLETED,
        JobRunStatus.SKIPPED_NON_TRADING_DAY,
    ],
)
def test_run_once_prints_machine_readable_status(
    status: JobRunStatus,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    service = StubReportService(status=status)
    monkeypatch.setattr(
        cli,
        "parse_args",
        lambda: argparse.Namespace(command="run-once", phase="a_share_close"),
    )
    monkeypatch.setattr(cli, "build_report_service", lambda: service)

    cli.main()

    assert json.loads(capsys.readouterr().out) == {
        "status": status.value,
        "phase": MarketPhase.A_SHARE_CLOSE.value,
    }
    assert service.phases == [MarketPhase.A_SHARE_CLOSE]


def test_run_once_does_not_swallow_service_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    service = StubReportService(error=RuntimeError("report failed"))
    monkeypatch.setattr(
        cli,
        "parse_args",
        lambda: argparse.Namespace(command="run-once", phase="a_share_close"),
    )
    monkeypatch.setattr(cli, "build_report_service", lambda: service)

    with pytest.raises(RuntimeError, match="report failed"):
        cli.main()

    assert capsys.readouterr().out == ""


def test_run_once_does_not_swallow_configuration_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli,
        "parse_args",
        lambda: argparse.Namespace(command="run-once", phase="a_share_close"),
    )

    def raise_configuration_error() -> StubReportService:
        raise RuntimeError("invalid configuration")

    monkeypatch.setattr(cli, "build_report_service", raise_configuration_error)

    with pytest.raises(RuntimeError, match="invalid configuration"):
        cli.main()

    assert capsys.readouterr().out == ""
