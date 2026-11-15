#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

try:
    from tools.gen_resources import MIXED_TAR_PATH
    from tools.verify_resources import load_tar_entries, max_kind_streaks
except ImportError:  # pragma: no cover
    import sys

    sys.path.append(str(Path(__file__).resolve().parents[1]))
    from tools.gen_resources import MIXED_TAR_PATH  # type: ignore
    from tools.verify_resources import load_tar_entries, max_kind_streaks  # type: ignore


def main() -> None:
    entries = load_tar_entries(MIXED_TAR_PATH)
    preview = [
        {
            "index": index,
            "name": entry.name,
            "kind": entry.kind,
            "size_bytes": entry.size_bytes,
        }
        for index, entry in enumerate(entries[:40])
    ]
    payload = {
        "mixed_tar": str(MIXED_TAR_PATH),
        "entry_count": len(entries),
        "max_kind_streaks": max_kind_streaks(entry.kind for entry in entries),
        "preview": preview,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
