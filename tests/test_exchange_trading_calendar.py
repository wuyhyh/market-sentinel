from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, Never, cast
from zoneinfo import ZoneInfo

import pytest
import yaml
from pydantic import BaseModel, Field, HttpUrl

from market_sentinel.domain.models import TradingMarket
from market_sentinel.trading_calendar.exchange import (
    MARKET_CALENDAR_CODES,
    ExchangeCalendarsTradingCalendar,
    TradingCalendarOutOfBoundsError,
    TradingCalendarProviderError,
    UnknownTradingMarketError,
)


class OfficialDateRecord(BaseModel):
    value: date = Field(alias="date")
    source_name: str = Field(min_length=1)
    source_url: HttpUrl


class MarketCalendarFixture(BaseModel):
    timezone: str = Field(min_length=1)
    known_trading_days: list[OfficialDateRecord] = Field(min_length=1)
    weekends: list[OfficialDateRecord] = Field(min_length=1)
    official_holidays: list[OfficialDateRecord] = Field(min_length=1)


class OfficialCalendarFixture(BaseModel):
    year: Literal[2026]
    markets: dict[TradingMarket, MarketCalendarFixture]


def _load_official_calendar_fixture() -> OfficialCalendarFixture:
    fixture_path = (
        Path(__file__).parent / "fixtures" / "trading_calendar_2026.yaml"
    )
    fixture_data = yaml.safe_load(fixture_path.read_text(encoding="utf-8"))
    return OfficialCalendarFixture.model_validate(fixture_data)


OFFICIAL_CALENDAR_FIXTURE = _load_official_calendar_fixture()

OFFICIAL_DATE_CASES: list[
    tuple[TradingMarket, str, str, OfficialDateRecord, bool]
] = []
OFFICIAL_DATE_CASE_IDS: list[str] = []

for fixture_market, market_fixture in OFFICIAL_CALENDAR_FIXTURE.markets.items():
    categories = (
        ("trading_day", market_fixture.known_trading_days, True),
        ("weekend", market_fixture.weekends, False),
        ("official_holiday", market_fixture.official_holidays, False),
    )
    for category, records, expected_is_trading_day in categories:
        for record in records:
            OFFICIAL_DATE_CASES.append(
                (
                    fixture_market,
                    market_fixture.timezone,
                    category,
                    record,
                    expected_is_trading_day,
                )
            )
            OFFICIAL_DATE_CASE_IDS.append(
                f"{fixture_market.value}-{category}-{record.value.isoformat()}"
            )


@pytest.fixture(scope="module")
def calendar() -> ExchangeCalendarsTradingCalendar:
    return ExchangeCalendarsTradingCalendar()


def test_official_fixture_covers_each_market_and_date_category() -> None:
    assert set(OFFICIAL_CALENDAR_FIXTURE.markets) == set(TradingMarket)

    for market_fixture in OFFICIAL_CALENDAR_FIXTURE.markets.values():
        records = (
            *market_fixture.known_trading_days,
            *market_fixture.weekends,
            *market_fixture.official_holidays,
        )

        assert records
        assert all(record.value.year == OFFICIAL_CALENDAR_FIXTURE.year for record in records)
        assert all(record.source_name.strip() for record in records)
        assert all(str(record.source_url).startswith("https://") for record in records)


@pytest.mark.parametrize(
    ("market", "timezone_name", "category", "record", "expected_is_trading_day"),
    OFFICIAL_DATE_CASES,
    ids=OFFICIAL_DATE_CASE_IDS,
)
def test_real_calendar_matches_official_date_fixture(
    calendar: ExchangeCalendarsTradingCalendar,
    market: TradingMarket,
    timezone_name: str,
    category: str,
    record: OfficialDateRecord,
    expected_is_trading_day: bool,
) -> None:
    del category  # Included in the parametrized case ID for readable failures.
    local_noon = datetime(
        record.value.year,
        record.value.month,
        record.value.day,
        12,
        tzinfo=ZoneInfo(timezone_name),
    )

    assert calendar.is_trading_day(market, local_noon) is expected_is_trading_day


@pytest.mark.parametrize(
    ("market", "expected_code"),
    [
        (TradingMarket.A_SHARE, "XSHG"),
        (TradingMarket.KOREA, "XKRX"),
        (TradingMarket.US, "XNYS"),
    ],
)
def test_each_market_maps_to_expected_calendar_code(
    market: TradingMarket,
    expected_code: str,
) -> None:
    assert MARKET_CALENDAR_CODES[market] == expected_code


def test_korea_official_election_closure_overrides_library(
    calendar: ExchangeCalendarsTradingCalendar,
) -> None:
    election_day = datetime(2026, 6, 3, 12, tzinfo=ZoneInfo("Asia/Seoul"))

    assert not calendar.is_trading_day(TradingMarket.KOREA, election_day)


def test_same_utc_moment_uses_each_markets_local_date(
    calendar: ExchangeCalendarsTradingCalendar,
) -> None:
    friday_utc_after_asia_midnight = datetime(2026, 7, 24, 16, 30, tzinfo=UTC)

    assert not calendar.is_trading_day(
        TradingMarket.A_SHARE,
        friday_utc_after_asia_midnight,
    )
    assert not calendar.is_trading_day(
        TradingMarket.KOREA,
        friday_utc_after_asia_midnight,
    )
    assert calendar.is_trading_day(
        TradingMarket.US,
        friday_utc_after_asia_midnight,
    )


def test_unknown_market_is_rejected(
    calendar: ExchangeCalendarsTradingCalendar,
) -> None:
    unknown_market = cast(TradingMarket, "unsupported")

    with pytest.raises(UnknownTradingMarketError, match="Unknown trading market"):
        calendar.is_trading_day(
            unknown_market,
            datetime(2026, 7, 24, 12, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2025, 12, 31, 12, tzinfo=UTC),
        datetime(2027, 1, 1, 12, tzinfo=UTC),
    ],
)
def test_unverified_date_is_rejected_as_out_of_bounds(
    calendar: ExchangeCalendarsTradingCalendar,
    moment: datetime,
) -> None:
    with pytest.raises(TradingCalendarOutOfBoundsError, match="verified range"):
        calendar.is_trading_day(TradingMarket.A_SHARE, moment)


def test_naive_datetime_is_rejected(
    calendar: ExchangeCalendarsTradingCalendar,
) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        calendar.is_trading_day(
            TradingMarket.A_SHARE,
            datetime(2026, 7, 24, 12),  # noqa: DTZ001 - deliberately tests rejection
        )


def test_calendar_initialization_failure_is_explicit() -> None:
    def failing_loader(calendar_code: str) -> Never:
        raise RuntimeError(f"failed to load {calendar_code}")

    with pytest.raises(
        TradingCalendarProviderError,
        match="Unable to initialize trading calendar XSHG",
    ) as exc_info:
        ExchangeCalendarsTradingCalendar(calendar_loader=failing_loader)

    assert isinstance(exc_info.value.__cause__, RuntimeError)


def test_calendar_query_failure_is_explicit() -> None:
    class FailingCalendar:
        def is_session(self, session_date: date) -> bool:
            raise RuntimeError(f"failed to query {session_date}")

    calendar = ExchangeCalendarsTradingCalendar(
        calendar_loader=lambda _: FailingCalendar(),
    )

    with pytest.raises(
        TradingCalendarProviderError,
        match="Unable to query trading calendar XSHG",
    ) as exc_info:
        calendar.is_trading_day(
            TradingMarket.A_SHARE,
            datetime(2026, 7, 24, 12, tzinfo=UTC),
        )

    assert isinstance(exc_info.value.__cause__, RuntimeError)
