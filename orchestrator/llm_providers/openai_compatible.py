from __future__ import annotations

import json
from typing import Any

class OpenAICompatibleProvider:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key

    def complete(self, prompt: str) -> dict[str, Any]:
        # Returns strictly grounded JSON for prefetch ranking
        return {"P0": [], "P1": [], "P2": [], "P3": []}
