import httpx

from market_sentinel.domain.models import MarketBrief
from market_sentinel.notifications.base import Notifier


class WebhookNotifier(Notifier):
    def __init__(self, url: str) -> None:
        self._url = url

    async def send(self, brief: MarketBrief) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                self._url,
                json=brief.model_dump(mode="json"),
            )
            response.raise_for_status()
