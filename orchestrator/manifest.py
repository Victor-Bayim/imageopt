from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

@dataclass
class StageResult:
    status: str
    artifacts: list[str] = field(default_factory=list)
    message: str = ""

class Manifest:
    def __init__(self, path: Path):
        self.path = path
        self.data = {}
        if path.exists():
            self.data = json.loads(path.read_text())

    def save(self):
        self.path.write_text(json.dumps(self.data, indent=2))

    def mark_passed(self, stage: str):
        self.data.setdefault("stages", {})[stage] = {"status": "passed"}
        self.save()
