import asyncio
import json

from openai import OpenAI

from market_sentinel.domain.models import MarketBrief, MarketSnapshot, RiskReport
from market_sentinel.llm.base import LLMAnalyst
from market_sentinel.llm.prompts import SYSTEM_PROMPT


class DeepSeekAnalyst(LLMAnalyst):
    def __init__(self, *, api_key: str, model: str, base_url: str) -> None:
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model

    async def analyze(
        self,
        snapshot: MarketSnapshot,
        risk_report: RiskReport,
    ) -> MarketBrief:
        payload = {
            "snapshot": snapshot.model_dump(mode="json"),
            "risk_report": risk_report.model_dump(mode="json"),
            "required_json_schema": MarketBrief.model_json_schema(),
        }

        def _call() -> MarketBrief:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {
                        "role": "system",
                        "content": SYSTEM_PROMPT
                        + "\n必须返回符合给定 JSON Schema 的 JSON 对象。",
                    },
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False),
                    },
                ],
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content
            if not content:
                raise RuntimeError("DeepSeek returned empty JSON content")
            return MarketBrief.model_validate_json(content)

        return await asyncio.to_thread(_call)
