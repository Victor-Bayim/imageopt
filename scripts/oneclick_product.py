#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Main recoverable pipeline entry
# Stages: preflight -> sample -> export-enrich -> analyze -> (llm-optimize) -> apply -> benchmark -> report

def main():
    parser = argparse.ArgumentParser(description="imageopt pipeline")
    sub = parser.add_subparsers(dest="cmd")
    run_p = sub.add_parser("run")
    run_p.add_argument("image")
    run_p.add_argument("--work-dir", required=True)
    run_p.add_argument("--mode", default="deterministic", choices=["deterministic", "llm-auto", "llm-manual"])
    args = parser.parse_args()
    if args.cmd == "run":
        print(f"Starting pipeline for {args.image} in {args.work_dir} (mode={args.mode})")
        # Full orchestration delegates to orchestrator.stages.*
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
