#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import posixpath
from pathlib import Path


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _norm_path(value) -> str:
    path = str(value or "").strip()
    if not path.startswith("/"):
        return ""
    return posixpath.normpath(path)


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _load_candidates(path: Path) -> list[str]:
    payload = _load_json(path)
    rows = payload if isinstance(payload, list) else list((payload or {}).get("candidates") or [])
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        norm = _norm_path(row.get("path"))
        if norm and norm not in seen:
            out.append(norm)
            seen.add(norm)
    return out


def _load_selected(path: Path) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        norm = _norm_path(line)
        if norm and norm not in seen:
            out.append(norm)
            seen.add(norm)
    return out


def _extract_entries(payload) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("entries", "files"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    toc = payload.get("toc")
    if isinstance(toc, dict):
        rows = toc.get("entries")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _normalize_entry(entry: dict) -> dict | None:
    path = _norm_path(entry.get("path") or entry.get("name"))
    if not path:
        return None
    layer_digest = str(
        entry.get("layer_digest")
        or entry.get("layer")
        or entry.get("digest")
        or entry.get("blob")
        or ""
    )
    entry_size = _safe_int(entry.get("size_bytes") or entry.get("size"))
    chunks_raw = entry.get("chunks")
    chunks: list[dict] = []
    if isinstance(chunks_raw, list) and chunks_raw:
        for index, raw in enumerate(chunks_raw):
            if not isinstance(raw, dict):
                continue
            size = _safe_int(
                raw.get("size")
                or raw.get("chunk_size")
                or raw.get("uncompressed_size")
                or raw.get("bytes")
            )
            if size <= 0:
                continue
            compressed_size = _safe_int(
                raw.get("compressed_size")
                or raw.get("compressed_bytes")
                or raw.get("compressed")
                or size
            )
            offset = _safe_int(raw.get("offset") or raw.get("chunk_offset"), index * size)
            chunk_layer = str(raw.get("layer_digest") or raw.get("layer") or layer_digest)
            chunks.append(
                {
                    "key": f"{chunk_layer}:{offset}:{size}:{compressed_size}",
                    "layer_digest": chunk_layer,
                    "offset": offset,
                    "size": size,
                    "compressed_size": compressed_size,
                }
            )
    elif entry_size > 0:
        chunks.append(
            {
                "key": f"{layer_digest}:0:{entry_size}:{entry_size}",
                "layer_digest": layer_digest,
                "offset": 0,
                "size": entry_size,
                "compressed_size": entry_size,
            }
        )
    if not chunks:
        return None
    return {
        "path": path,
        "layer_digest": layer_digest,
        "size_bytes": entry_size,
        "chunks": chunks,
    }


def _build_toc_index(path: Path) -> dict[str, dict]:
    payload = _load_json(path)
    index: dict[str, dict] = {}
    for entry in _extract_entries(payload):
        normalized = _normalize_entry(entry)
        if normalized is not None:
            index[normalized["path"]] = normalized
    return index


def build_shadow(*, toc_index: dict[str, dict], candidates: list[str], selected: list[str], top_shared: int) -> dict:
    selected_chunk_paths: dict[str, set[str]] = {}
    selected_chunk_meta: dict[str, dict] = {}
    selected_unique_layers: set[str] = set()
    selected_details: list[dict] = []
    selected_missing: list[str] = []
    selected_path_sum_bytes = 0
    selected_path_sum_compressed_bytes = 0

    for path in selected:
        entry = toc_index.get(path)
        if entry is None:
            selected_missing.append(path)
            continue
        path_chunk_bytes = 0
        path_chunk_compressed_bytes = 0
        path_layers: set[str] = set()
        for chunk in entry["chunks"]:
            key = chunk["key"]
            selected_chunk_paths.setdefault(key, set()).add(path)
            selected_chunk_meta.setdefault(key, chunk)
            if chunk.get("layer_digest"):
                selected_unique_layers.add(chunk["layer_digest"])
                path_layers.add(chunk["layer_digest"])
            path_chunk_bytes += int(chunk["size"])
            path_chunk_compressed_bytes += int(chunk["compressed_size"])
        selected_path_sum_bytes += path_chunk_bytes
        selected_path_sum_compressed_bytes += path_chunk_compressed_bytes
        selected_details.append(
            {
                "path": path,
                "found": True,
                "layer_digests": sorted(path_layers),
                "chunk_count": len(entry["chunks"]),
                "estimated_bytes": path_chunk_bytes,
                "estimated_compressed_bytes": path_chunk_compressed_bytes,
            }
        )

    candidate_found = sum(1 for path in candidates if path in toc_index)
    candidate_missing = max(len(candidates) - candidate_found, 0)
    candidate_found_ratio = (
        float(candidate_found) / float(len(candidates))
        if candidates
        else 0.0
    )
    unique_chunk_bytes = sum(int(meta["size"]) for meta in selected_chunk_meta.values())
    unique_chunk_compressed_bytes = sum(
        int(meta.get("compressed_size") or meta["size"]) for meta in selected_chunk_meta.values()
    )
    overlap_bytes = max(selected_path_sum_bytes - unique_chunk_bytes, 0)
    overlap_compressed_bytes = max(selected_path_sum_compressed_bytes - unique_chunk_compressed_bytes, 0)
    overlap_ratio = (
        float(overlap_bytes) / float(selected_path_sum_bytes)
        if selected_path_sum_bytes > 0
        else 0.0
    )
    overlap_compressed_ratio = (
        float(overlap_compressed_bytes) / float(selected_path_sum_compressed_bytes)
        if selected_path_sum_compressed_bytes > 0
        else 0.0
    )
    shared_chunks = [
        {
            "chunk_key": key,
            "layer_digest": meta.get("layer_digest"),
            "offset": int(meta.get("offset") or 0),
            "size": int(meta.get("size") or 0),
            "compressed_size": int(meta.get("compressed_size") or meta.get("size") or 0),
            "path_count": len(paths),
            "paths": sorted(paths),
        }
        for key, paths in selected_chunk_paths.items()
        if len(paths) > 1
        for meta in [selected_chunk_meta[key]]
    ]
    shared_chunks.sort(key=lambda row: (-row["path_count"], -row["compressed_size"], row["chunk_key"]))
    shared_chunk_bytes = sum(row["size"] for row in shared_chunks)
    shared_chunk_compressed_bytes = sum(row["compressed_size"] for row in shared_chunks)
    selected_found_ratio = (
        float(len(selected_details)) / float(len(selected))
        if selected
        else 0.0
    )
    warnings: list[str] = []
    if candidate_missing > 0:
        warnings.append(
            f"{candidate_missing} candidate path(s) were not found in image TOC"
        )
    if selected_missing:
        warnings.append(
            f"{len(selected_missing)} selected prefetch path(s) were not found in image TOC"
        )
    if len(selected_details) >= 2 and overlap_ratio >= 0.25:
        warnings.append(
            f"selected paths show high chunk overlap ({overlap_ratio:.2%} of path-sum bytes)"
        )

    return {
        "audit_version": 1,
        "toc_entry_count": len(toc_index),
        "candidate_paths_total": len(candidates),
        "candidate_paths_found": candidate_found,
        "candidate_paths_missing": candidate_missing,
        "candidate_paths_found_ratio": candidate_found_ratio,
        "selected_paths_total": len(selected),
        "selected_paths_found": len(selected_details),
        "selected_paths_missing": len(selected_missing),
        "selected_paths_found_ratio": selected_found_ratio,
        "selected_unique_layer_count": len(selected_unique_layers),
        "selected_unique_chunk_count": len(selected_chunk_meta),
        "selected_shared_chunk_count": len(shared_chunks),
        "selected_path_sum_bytes": selected_path_sum_bytes,
        "selected_path_sum_compressed_bytes": selected_path_sum_compressed_bytes,
        "selected_estimated_bytes": unique_chunk_bytes,
        "selected_estimated_compressed_bytes": unique_chunk_compressed_bytes,
        "selected_shared_chunk_bytes": shared_chunk_bytes,
        "selected_shared_chunk_compressed_bytes": shared_chunk_compressed_bytes,
        "selected_overlap_bytes": overlap_bytes,
        "selected_overlap_compressed_bytes": overlap_compressed_bytes,
        "selected_overlap_ratio": overlap_ratio,
        "selected_overlap_compressed_ratio": overlap_compressed_ratio,
        "selected_missing_paths": selected_missing,
        "selected_missing_paths_preview": selected_missing[:5],
        "selected": selected_details,
        "shared_chunks_top": shared_chunks[: max(top_shared, 0)],
        "warnings": warnings,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a TOC shadow audit for selected prefetch paths.")
    parser.add_argument("--toc-json", required=True, help="path to image_toc.json-like payload")
    parser.add_argument("--candidates", required=True, help="path to candidates.json")
    parser.add_argument("--selected-list", required=True, help="path to estargz.list")
    parser.add_argument("--out", required=True, help="path to toc_shadow.json")
    parser.add_argument("--top-shared", type=int, default=10, help="number of top shared chunks to retain")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    toc_path = Path(args.toc_json)
    candidates_path = Path(args.candidates)
    selected_path = Path(args.selected_list)
    out_path = Path(args.out)

    toc_index = _build_toc_index(toc_path)
    payload = build_shadow(
        toc_index=toc_index,
        candidates=_load_candidates(candidates_path),
        selected=_load_selected(selected_path),
        top_shared=args.top_shared,
    )
    payload["toc_source_path"] = str(toc_path)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
