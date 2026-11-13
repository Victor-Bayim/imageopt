from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orchestrator.manifest import StageResult


def run(work_dir: Path, config: dict[str, Any]) -> StageResult:
    return StageResult(status="passed", artifacts=[])
