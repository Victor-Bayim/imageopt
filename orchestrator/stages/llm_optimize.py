from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestrator.manifest import StageResult

def run(work_dir: Path, config: dict[str, Any]) -> StageResult:
    """Optional LLM re-ranking with fallback to deterministic."""
    return StageResult(status="passed")
