from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import date

from market_sentinel.domain.security_data import DailyBarBatch, SecurityMasterBatch


class SecurityMasterProvider(ABC):
    @abstractmethod
    async def get_security_master(self, symbols: Sequence[str]) -> SecurityMasterBatch:
        """Return provider-independent reference data for canonical symbols."""


class DailyBarProvider(ABC):
    @abstractmethod
    async def get_daily_bars(
        self,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> DailyBarBatch:
        """Return unadjusted or explicitly labelled daily bars for canonical symbols."""
