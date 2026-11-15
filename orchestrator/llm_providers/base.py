from __future__ import annotations

from abc import ABC, abstractmethod

from orchestrator.llm_providers.types import LLMRequest, LLMResponse


class LLMProvider(ABC):
    @abstractmethod
    def complete(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError
