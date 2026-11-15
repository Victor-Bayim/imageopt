#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

try:
    from tools.gen_resources import COLDS_PER_HOT, MIXED_TAR_PATH
    from tools.verify_resources import hot_positions, load_tar_entries, max_kind_streaks
except ImportError:  # pragma: no cover
    import sys

    sys.path.append(str(Path(__file__).resolve().parents[1]))
    from tools.gen_resources import COLDS_PER_HOT, MIXED_TAR_PATH  # type: ignore
    from tools.verify_resources import hot_positions, load_tar_entries, max_kind_streaks  # type: ignore


def main() -> None:
    entries = load_tar_entries(MIXED_TAR_PATH)
    positions = hot_positions(entries)
    gaps = [curr - prev for prev, curr in zip(positions, positions[1:])]
    preview = [
        {
            "index": index,
            "name": entry.name,
            "kind": entry.kind,
            "size_bytes": entry.size_bytes,
        }
        for index, entry in enumerate(entries[:96])
    ]
    payload = {
        "mixed_tar": str(MIXED_TAR_PATH),
        "entry_count": len(entries),
        "colds_per_hot": COLDS_PER_HOT,
        "max_kind_streaks": max_kind_streaks(entry.kind for entry in entries),
        "hot_position_preview": positions[:12],
        "hot_gap_preview": gaps[:12],
        "preview": preview,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
