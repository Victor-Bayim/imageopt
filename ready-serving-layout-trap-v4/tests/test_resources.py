from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.gen_resources import (
    COLD_FILE_COUNT,
    HOT_FILE_COUNT,
    LATE_FILE_COUNT,
    build_layout_entries,
    build_startup_plan,
)
from tools.verify_resources import max_kind_streaks


def test_layout_entry_counts_match_v4_contract():
    entries = build_layout_entries()
    assert HOT_FILE_COUNT == sum(1 for entry in entries if entry.kind == "hot")
    assert COLD_FILE_COUNT == sum(1 for entry in entries if entry.kind == "cold")
    assert LATE_FILE_COUNT == sum(1 for entry in entries if entry.kind == "late")


def test_layout_entry_order_is_interleaved_for_hot_cold_late():
    entries = build_layout_entries()
    streaks = max_kind_streaks(entry.kind for entry in entries)
    assert streaks["hot"] == 1
    assert streaks["cold"] == 3
    assert streaks["late"] == 1
    assert {"hot", "cold", "late"} <= {entry.kind for entry in entries[:20]}


def test_startup_plan_hot_paths_exist_in_layout():
    entries = build_layout_entries()
    plan = build_startup_plan(entries)
    entry_paths = {entry.abs_path for entry in entries}
    assert plan["hot_paths"]
    assert set(plan["hot_paths"]).issubset(entry_paths)
    assert set(plan["late_predict_paths"]).issubset(entry_paths)
