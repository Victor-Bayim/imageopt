from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.gen_resources import (  # noqa: E402
    COLD_FILE_COUNT,
    COLDS_PER_HOT,
    HOT_FILE_COUNT,
    LATE_FILE_COUNT,
    build_layout_entries,
    build_startup_plan,
)
from tools.verify_resources import max_kind_streaks  # noqa: E402


def test_layout_entry_counts_match_v5_contract():
    entries = build_layout_entries()
    assert HOT_FILE_COUNT == sum(1 for entry in entries if entry.kind == "hot")
    assert COLD_FILE_COUNT == sum(1 for entry in entries if entry.kind == "cold")
    assert LATE_FILE_COUNT == sum(1 for entry in entries if entry.kind == "late")


def test_layout_entry_order_spreads_hot_entries_across_cold_groups():
    entries = build_layout_entries()
    streaks = max_kind_streaks(entry.kind for entry in entries)
    assert streaks["hot"] == 1
    assert streaks["cold"] == COLDS_PER_HOT
    assert streaks["late"] == 1

    hot_positions = [index for index, entry in enumerate(entries) if entry.kind == "hot"]
    assert hot_positions[0] == COLDS_PER_HOT
    assert len(hot_positions) == HOT_FILE_COUNT
    assert all(curr - prev == COLDS_PER_HOT + 1 for prev, curr in zip(hot_positions, hot_positions[1:]))


def test_startup_plan_hot_paths_exist_in_layout():
    entries = build_layout_entries()
    plan = build_startup_plan(entries)
    entry_paths = {entry.abs_path for entry in entries}
    assert len(plan["hot_paths"]) == HOT_FILE_COUNT
    assert set(plan["hot_paths"]).issubset(entry_paths)
    assert set(plan["late_predict_paths"]).issubset(entry_paths)
