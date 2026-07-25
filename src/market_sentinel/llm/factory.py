from market_sentinel.config import Settings
from market_sentinel.llm.base import LLMAnalyst
from market_sentinel.llm.deepseek_provider import DeepSeekAnalyst
from market_sentinel.llm.mock_provider import MockAnalyst
from market_sentinel.llm.openai_provider import OpenAIAnalyst


def build_analyst(settings: Settings) -> LLMAnalyst:
    if settings.llm_provider == "openai":
        if settings.openai_api_key is None:
            raise RuntimeError("OPENAI_API_KEY is required")
        return OpenAIAnalyst(
            api_key=settings.openai_api_key.get_secret_value(),
            model=settings.openai_model,
        )

    if settings.llm_provider == "deepseek":
        if settings.deepseek_api_key is None:
            raise RuntimeError("DEEPSEEK_API_KEY is required")
        return DeepSeekAnalyst(
            api_key=settings.deepseek_api_key.get_secret_value(),
            model=settings.deepseek_model,
            base_url=settings.deepseek_base_url,
        )

    return MockAnalyst()
