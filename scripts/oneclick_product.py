#!/usr/bin/env python3
"""Main entry for the imageopt pipeline.

Usage:
  python3 scripts/oneclick_product.py run <IMAGE> --work-dir /tmp/run
  python3 scripts/oneclick_product.py report --work-dir /tmp/run
"""
from __future__ import annotations

import argparse
from pathlib import Path

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "report", "sample"])
    parser.add_argument("image", nargs="?")
    parser.add_argument("--work-dir", required=True)
    args = parser.parse_args()
    print(f"Running {args.command} on {args.image} in {args.work_dir}")
    # Full stage orchestration lives in orchestrator/

if __name__ == "__main__":
    main()
