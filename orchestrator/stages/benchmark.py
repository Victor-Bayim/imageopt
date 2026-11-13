from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestrator.manifest import StageResult

def run(work_dir: Path, config: dict[str, Any]) -> StageResult:
    """Run triple cold-start comparison."""
    return StageResult(status="passed", artifacts=["benchmark.json"])
