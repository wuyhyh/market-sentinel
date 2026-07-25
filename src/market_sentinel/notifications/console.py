import json

from market_sentinel.domain.models import MarketBrief
from market_sentinel.notifications.base import Notifier


class ConsoleNotifier(Notifier):
    async def send(self, brief: MarketBrief) -> None:
        print(json.dumps(brief.model_dump(mode="json"), ensure_ascii=False, indent=2))
