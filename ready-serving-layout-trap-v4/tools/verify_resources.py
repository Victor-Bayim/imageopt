#!/usr/bin/env python3
from __future__ import annotations

import json
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    from tools.gen_resources import (
        CONTROL_DIR,
        HOT_FILE_COUNT,
        HOT_FILE_SIZE,
        COLD_FILE_COUNT,
        COLD_FILE_SIZE,
        LATE_FILE_COUNT,
        LATE_FILE_SIZE,
        MIXED_TAR_PATH,
        build_layout_entries,
    )
except ImportError:  # pragma: no cover
    import sys

    sys.path.append(str(Path(__file__).resolve().parents[1]))
    from tools.gen_resources import (  # type: ignore
        CONTROL_DIR,
        HOT_FILE_COUNT,
        HOT_FILE_SIZE,
        COLD_FILE_COUNT,
        COLD_FILE_SIZE,
        LATE_FILE_COUNT,
        LATE_FILE_SIZE,
        MIXED_TAR_PATH,
        build_layout_entries,
    )


@dataclass(frozen=True)
class TarEntryInfo:
    name: str
    kind: str
    size_bytes: int


def kind_from_name(name: str) -> str:
    if "_hot_" in name:
        return "hot"
    if "_cold_" in name:
        return "cold"
    if "_late_" in name:
        return "late"
    raise ValueError(f"unrecognized layout kind for {name}")


def load_tar_entries(path: Path) -> list[TarEntryInfo]:
    with tarfile.open(path, "r") as archive:
        return [
            TarEntryInfo(
                name=member.name,
                kind=kind_from_name(member.name),
                size_bytes=member.size,
            )
            for member in archive.getmembers()
            if member.isfile()
        ]


def max_kind_streaks(kinds: Iterable[str]) -> dict[str, int]:
    max_streak = {"hot": 0, "cold": 0, "late": 0}
    current_kind = None
    current_len = 0
    for kind in kinds:
        if kind == current_kind:
            current_len += 1
        else:
            if current_kind is not None and current_len > max_streak[current_kind]:
                max_streak[current_kind] = current_len
            current_kind = kind
            current_len = 1
    if current_kind is not None and current_len > max_streak[current_kind]:
        max_streak[current_kind] = current_len
    return max_streak


def verify_resources(root: Path | None = None) -> dict[str, object]:
    del root
    startup_plan_path = CONTROL_DIR / "startup_plan.json"
    layout_stats_path = CONTROL_DIR / "layout_stats.json"
    slot_map_path = CONTROL_DIR / "slot_map.csv"

    if not startup_plan_path.is_file():
        raise RuntimeError(f"missing {startup_plan_path}")
    if not layout_stats_path.is_file():
        raise RuntimeError(f"missing {layout_stats_path}")
    if not slot_map_path.is_file():
        raise RuntimeError(f"missing {slot_map_path}")
    if not MIXED_TAR_PATH.is_file():
        raise RuntimeError(f"missing {MIXED_TAR_PATH}")

    planned_paths = {entry.abs_path for entry in build_layout_entries()}
    startup_plan = json.loads(startup_plan_path.read_text(encoding="utf-8"))
    tar_entries = load_tar_entries(MIXED_TAR_PATH)
    kinds = [entry.kind for entry in tar_entries]

    hot_entries = [entry for entry in tar_entries if entry.kind == "hot"]
    cold_entries = [entry for entry in tar_entries if entry.kind == "cold"]
    late_entries = [entry for entry in tar_entries if entry.kind == "late"]

    if len(hot_entries) != HOT_FILE_COUNT:
        raise RuntimeError(f"hot count mismatch: {len(hot_entries)} != {HOT_FILE_COUNT}")
    if len(cold_entries) != COLD_FILE_COUNT:
        raise RuntimeError(f"cold count mismatch: {len(cold_entries)} != {COLD_FILE_COUNT}")
    if len(late_entries) != LATE_FILE_COUNT:
        raise RuntimeError(f"late count mismatch: {len(late_entries)} != {LATE_FILE_COUNT}")

    if any(entry.size_bytes != HOT_FILE_SIZE for entry in hot_entries):
        raise RuntimeError("hot file size mismatch detected")
    if any(entry.size_bytes != COLD_FILE_SIZE for entry in cold_entries):
        raise RuntimeError("cold file size mismatch detected")
    if any(entry.size_bytes != LATE_FILE_SIZE for entry in late_entries):
        raise RuntimeError("late file size mismatch detected")

    missing_hot_paths = [path for path in startup_plan.get("hot_paths", []) if path not in planned_paths]
    if missing_hot_paths:
        raise RuntimeError(f"startup_plan hot paths missing from tar plan: {missing_hot_paths[:3]}")

    streaks = max_kind_streaks(kinds)
    if streaks["hot"] > 1:
        raise RuntimeError(f"hot entries are unexpectedly clustered: max streak {streaks['hot']}")
    if streaks["cold"] > 3:
        raise RuntimeError(f"cold entries are unexpectedly clustered: max streak {streaks['cold']}")
    if streaks["late"] > 1:
        raise RuntimeError(f"late entries are unexpectedly clustered: max streak {streaks['late']}")

    return {
        "hot_files": len(hot_entries),
        "cold_files": len(cold_entries),
        "late_files": len(late_entries),
        "hot_bytes": sum(entry.size_bytes for entry in hot_entries),
        "cold_bytes": sum(entry.size_bytes for entry in cold_entries),
        "late_bytes": sum(entry.size_bytes for entry in late_entries),
        "max_kind_streaks": streaks,
        "startup_hot_paths": len(startup_plan.get("hot_paths", [])),
    }


def main() -> None:
    summary = verify_resources()
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
