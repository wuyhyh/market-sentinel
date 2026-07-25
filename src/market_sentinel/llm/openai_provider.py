import asyncio
import json

from openai import OpenAI

from market_sentinel.domain.models import MarketBrief, MarketSnapshot, RiskReport
from market_sentinel.llm.base import LLMAnalyst
from market_sentinel.llm.prompts import SYSTEM_PROMPT


class OpenAIAnalyst(LLMAnalyst):
    def __init__(self, *, api_key: str, model: str) -> None:
        self._client = OpenAI(api_key=api_key)
        self._model = model

    async def analyze(
        self,
        snapshot: MarketSnapshot,
        risk_report: RiskReport,
    ) -> MarketBrief:
        payload = {
            "snapshot": snapshot.model_dump(mode="json"),
            "risk_report": risk_report.model_dump(mode="json"),
        }

        def _call() -> MarketBrief:
            response = self._client.responses.parse(
                model=self._model,
                input=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            "请根据以下 JSON 生成结构化市场简报：\n"
                            + json.dumps(payload, ensure_ascii=False)
                        ),
                    },
                ],
                text_format=MarketBrief,
            )
            if response.output_parsed is None:
                raise RuntimeError("OpenAI returned no parsed output")
            return response.output_parsed

        return await asyncio.to_thread(_call)
