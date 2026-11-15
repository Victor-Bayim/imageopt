#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESOURCES_DIR = PROJECT_ROOT / "resources"
CONTROL_DIR = RESOURCES_DIR / "control"
MIXED_TAR_PATH = RESOURCES_DIR / "mixed.tar"
LAYOUT_ROOT = "/srv/layout"
SEED = "ready-serving-layout-trap-v5-seed-v1"
STREAM_CHUNK_SIZE = 1024 * 1024

HOT_FILE_COUNT = 32
HOT_FILE_SIZE = 2 * 1024 * 1024
COLD_FILE_COUNT = 2048
COLD_FILE_SIZE = 4 * 1024 * 1024
LATE_FILE_COUNT = 1
LATE_FILE_SIZE = 64 * 1024
COLDS_PER_HOT = 64


@dataclass(frozen=True)
class LayoutEntry:
    slot: int
    kind: str
    logical_index: int
    size_bytes: int
    tar_name: str

    @property
    def abs_path(self) -> str:
        return f"{LAYOUT_ROOT}/{self.tar_name}"


def slot_filename(slot: int, kind: str, logical_index: int) -> str:
    return f"slot_{slot:06d}_{kind}_{logical_index:03d}.bin"


def make_entry(slot: int, kind: str, logical_index: int, size_bytes: int) -> LayoutEntry:
    return LayoutEntry(
        slot=slot,
        kind=kind,
        logical_index=logical_index,
        size_bytes=size_bytes,
        tar_name=slot_filename(slot, kind, logical_index),
    )


def build_layout_entries() -> list[LayoutEntry]:
    if COLD_FILE_COUNT % HOT_FILE_COUNT != 0:
        raise RuntimeError(
            f"cold/hot contract mismatch: {COLD_FILE_COUNT} is not divisible by {HOT_FILE_COUNT}"
        )
    if COLDS_PER_HOT != COLD_FILE_COUNT // HOT_FILE_COUNT:
        raise RuntimeError(
            f"configured COLDS_PER_HOT={COLDS_PER_HOT} does not match exact ratio {COLD_FILE_COUNT // HOT_FILE_COUNT}"
        )

    entries: list[LayoutEntry] = []
    slot = 0
    cold_index = 0

    for hot_index in range(HOT_FILE_COUNT):
        for _ in range(COLDS_PER_HOT):
            entries.append(make_entry(slot, "cold", cold_index, COLD_FILE_SIZE))
            cold_index += 1
            slot += 1
        entries.append(make_entry(slot, "hot", hot_index, HOT_FILE_SIZE))
        slot += 1

    if cold_index != COLD_FILE_COUNT:
        raise RuntimeError(f"cold count mismatch: {cold_index} != {COLD_FILE_COUNT}")

    for late_index in range(LATE_FILE_COUNT):
        entries.append(make_entry(slot, "late", late_index, LATE_FILE_SIZE))
        slot += 1

    return entries


def build_startup_plan(entries: list[LayoutEntry]) -> dict[str, object]:
    hot_paths = [entry.abs_path for entry in entries if entry.kind == "hot"]
    late_paths = [entry.abs_path for entry in entries if entry.kind == "late"]
    expected_hot_bytes = sum(entry.size_bytes for entry in entries if entry.kind == "hot")
    return {
        "hot_paths": hot_paths,
        "late_predict_paths": late_paths[:1],
        "expected_hot_bytes": expected_hot_bytes,
    }


def build_layout_stats(entries: list[LayoutEntry]) -> dict[str, object]:
    kind_counts = {kind: sum(1 for entry in entries if entry.kind == kind) for kind in ("hot", "cold", "late")}
    kind_bytes = {
        kind: sum(entry.size_bytes for entry in entries if entry.kind == kind)
        for kind in ("hot", "cold", "late")
    }
    return {
        "layout_root": LAYOUT_ROOT,
        "total_entries": len(entries),
        "kind_counts": kind_counts,
        "kind_bytes": kind_bytes,
        "colds_per_hot": COLDS_PER_HOT,
        "interleave_pattern": "64 cold entries followed by 1 hot entry, repeated; 1 late entry appended",
    }


def deterministic_chunk(label: str, chunk_index: int, size: int) -> bytes:
    token = f"{SEED}:{label}:{chunk_index}".encode("utf-8")
    return hashlib.shake_256(token).digest(size)


class DeterministicStream:
    def __init__(self, label: str, size_bytes: int) -> None:
        self.label = label
        self.remaining = size_bytes
        self.chunk_index = 0
        self.pending = b""

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        if size is None or size < 0 or size > self.remaining:
            size = self.remaining

        out = bytearray()
        while len(out) < size and self.remaining > 0:
            if not self.pending:
                chunk_size = min(STREAM_CHUNK_SIZE, self.remaining)
                self.pending = deterministic_chunk(self.label, self.chunk_index, chunk_size)
                self.chunk_index += 1
            take = min(size - len(out), len(self.pending))
            out.extend(self.pending[:take])
            self.pending = self.pending[take:]
            self.remaining -= take
        return bytes(out)


def ensure_directories() -> None:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_slot_map(path: Path, entries: list[LayoutEntry]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["slot", "kind", "logical_index", "path", "size_bytes"])
        for entry in entries:
            writer.writerow([entry.slot, entry.kind, entry.logical_index, entry.abs_path, entry.size_bytes])


def write_mixed_tar(path: Path, entries: list[LayoutEntry]) -> None:
    with tarfile.open(path, "w") as archive:
        for entry in entries:
            info = tarfile.TarInfo(name=entry.tar_name)
            info.size = entry.size_bytes
            info.mode = 0o644
            info.mtime = 0
            archive.addfile(info, fileobj=DeterministicStream(entry.tar_name, entry.size_bytes))


def generate_resources() -> dict[str, object]:
    ensure_directories()
    entries = build_layout_entries()
    startup_plan = build_startup_plan(entries)
    layout_stats = build_layout_stats(entries)

    write_json(CONTROL_DIR / "startup_plan.json", startup_plan)
    write_json(CONTROL_DIR / "layout_stats.json", layout_stats)
    write_slot_map(CONTROL_DIR / "slot_map.csv", entries)
    write_mixed_tar(MIXED_TAR_PATH, entries)

    return {
        "entries": len(entries),
        "mixed_tar": str(MIXED_TAR_PATH),
        "startup_plan": str(CONTROL_DIR / "startup_plan.json"),
        "slot_map": str(CONTROL_DIR / "slot_map.csv"),
        "layout_stats": str(CONTROL_DIR / "layout_stats.json"),
    }


def main() -> None:
    summary = generate_resources()
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
