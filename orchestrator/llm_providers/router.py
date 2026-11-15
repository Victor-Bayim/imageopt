from __future__ import annotations

from orchestrator.llm_providers.base import LLMProvider
from orchestrator.llm_providers.mock import MockProvider
from orchestrator.llm_providers.types import ProviderConfig


class ProviderRouter:
    def __init__(
        self,
        *,
        mock_mode: str = "success",
        mock_raw_text: str | None = None,
        mock_raw_payload: object | None = None,
    ) -> None:
        self._mock_mode = mock_mode
        self._mock_raw_text = mock_raw_text
        self._mock_raw_payload = mock_raw_payload

    def build(self, config: ProviderConfig) -> LLMProvider:
        if config.provider_type == "mock":
            return MockProvider(
                config,
                mode=self._mock_mode,
                raw_text=self._mock_raw_text,
                raw_payload=self._mock_raw_payload,
            )
        if config.provider_type == "openai_compatible":
            from orchestrator.llm_providers.openai_compatible import OpenAICompatibleProvider

            return OpenAICompatibleProvider(config)
        if config.provider_type == "openai_responses":
            from orchestrator.llm_providers.openai_responses import OpenAIResponsesProvider

            return OpenAIResponsesProvider(config)
        raise ValueError(f"unsupported provider type: {config.provider_type}")
