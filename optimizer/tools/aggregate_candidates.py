#!/usr/bin/env python3
"""Compatibility wrapper for the canonical aggregate_candidates entrypoint."""

from pathlib import Path
import runpy
import sys


def main() -> None:
    root_script = Path(__file__).resolve().parents[1] / "aggregate_candidates.py"
    if not root_script.is_file():
        raise SystemExit(f"missing canonical aggregate_candidates.py: {root_script}")
    sys.path.insert(0, str(root_script.parent))
    runpy.run_path(str(root_script), run_name="__main__")


if __name__ == "__main__":
    main()
